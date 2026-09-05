"""The mascot's expression for every (status, tone) pair.

This is the authority for both sides: `tools/gen_mascot_table.py` compiles it into
`firmware/src/mascot_table.h`, and `pc_app/render.py` draws the GUI preview from it. A test
asserts the checked-in header still matches, so the two cannot drift.

A face is authored as nine bases -- one per status, the honest reaction to that status -- plus
one modifier per tone. The tone is applied *after* the status, and deliberately overrides it:
that is the whole point of sarcasm mode. Being green does not stop the face implying you would
rather be left alone.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pc_app.i18n import TONES, normalise_tone
from pc_app.presence import Status

#: Idle animation styles. Must match the Idle enum in firmware/src/mascot.h, in this order.
IDLES = ("bob", "sway", "slump", "sleep", "bounce", "twitch")


@dataclass(frozen=True)
class Face:
    """One expression, as parameters rather than pixels, so the firmware can tween between them."""

    #: -100 furrowed and angry .. +100 raised and surprised.
    brow_tilt: int
    #: 0 shut .. 100 wide. Anything under ~50 reads as half-lidded, which is to say unimpressed.
    eye_open: int
    #: -100 frown .. +100 grin.
    mouth_curve: int
    #: 0 closed .. 100 wide open. A little of this reads as "talking".
    mouth_open: int
    #: One brow up and one down -- the single most legible "really?" cue we have.
    brow_asym: bool
    blush: bool
    idle: str

    def __post_init__(self) -> None:
        if self.idle not in IDLES:
            raise ValueError(f"unknown idle style {self.idle!r}")


#: The honest face for each status, before any tone is applied. Keyed by the lowercase status
#: name, which is also the caption folder name and the firmware's StatusTheme.folder.
BASE_FACES: dict[str, Face] = {
    "available": Face(brow_tilt=10, eye_open=100, mouth_curve=70, mouth_open=0,
                      brow_asym=False, blush=False, idle="bob"),
    "busy": Face(brow_tilt=-30, eye_open=85, mouth_curve=-10, mouth_open=0,
                 brow_asym=False, blush=False, idle="sway"),
    # Mouth slightly open: the one cue that reads as "is talking" at this size.
    "in_meeting": Face(brow_tilt=-10, eye_open=70, mouth_curve=0, mouth_open=35,
                       brow_asym=False, blush=False, idle="sway"),
    "dnd": Face(brow_tilt=-70, eye_open=60, mouth_curve=-60, mouth_open=0,
                brow_asym=False, blush=False, idle="slump"),
    "away": Face(brow_tilt=0, eye_open=20, mouth_curve=0, mouth_open=0,
                 brow_asym=False, blush=False, idle="sleep"),
    "brb": Face(brow_tilt=20, eye_open=45, mouth_curve=20, mouth_open=0,
                brow_asym=False, blush=False, idle="sleep"),
    "offline": Face(brow_tilt=-10, eye_open=0, mouth_curve=-20, mouth_open=0,
                    brow_asym=False, blush=False, idle="sleep"),
    # Raised brows and a slightly open mouth: confused rather than unhappy.
    "unknown": Face(brow_tilt=40, eye_open=90, mouth_curve=0, mouth_open=20,
                    brow_asym=False, blush=False, idle="twitch"),
    "disconnected": Face(brow_tilt=-20, eye_open=30, mouth_curve=-40, mouth_open=0,
                         brow_asym=False, blush=False, idle="twitch"),
}


def _normal(face: Face) -> Face:
    """Say what you see."""
    return face


def _sarcastic(face: Face) -> Face:
    """Unimpressed, whatever is actually going on.

    Half-lidded eyes, one brow up, and a mouth flattened to a smirk. Applied on top of a green
    status this is exactly the intended effect: technically available, visibly unenthusiastic.
    Eyes are clamped rather than set, so the sleeping statuses stay shut.
    """
    return replace(
        face,
        eye_open=min(face.eye_open, 45),
        brow_tilt=60,
        brow_asym=True,
        mouth_curve=-15,
        mouth_open=0,
        blush=False,
        idle="slump" if face.idle in ("bob", "bounce", "sway") else face.idle,
    )


def _retriever(face: Face) -> Face:
    """Delighted to help, whatever it costs. Overrides everything, including being asleep."""
    return replace(
        face,
        eye_open=100,
        brow_tilt=30,
        brow_asym=False,
        mouth_curve=90,
        mouth_open=40,
        blush=True,
        idle="bounce",
    )


#: Applied after the status base. Keys must cover TONES exactly.
TONE_MODIFIERS = {
    "normal": _normal,
    "sarcastic": _sarcastic,
    "retriever": _retriever,
}

#: Status names in firmware enum order, so the generated C table can be a flat array.
STATUS_NAMES = tuple(status.value.lower() for status in Status)


def face_for(status: str, tone: str = "normal") -> Face:
    """The expression for one (status, tone) pair. Unknown values fall back rather than raise."""
    base = BASE_FACES.get(status.lower(), BASE_FACES["unknown"])
    return TONE_MODIFIERS[normalise_tone(tone)](base)


def table() -> list[tuple[str, str, Face]]:
    """Every combination, in the order the generated firmware table indexes them."""
    return [(status, tone, face_for(status, tone)) for status in STATUS_NAMES for tone in TONES]
