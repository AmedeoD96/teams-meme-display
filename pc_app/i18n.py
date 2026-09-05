"""User-visible strings for the tray app, in every language the device supports.

The device has its own copies of the status labels (firmware/src/status.cpp) and its own caption
banks (captions/<lang>/), because it has to keep showing sensible text when the PC is gone. This
module is only for the tray menu and tooltip.
"""

from __future__ import annotations

from pc_app.presence import Status

#: Must match LANGUAGES in tools/build_memes.py and kLanguageCodes in firmware/src/status.cpp.
LANGUAGES = ("en", "it")
#: What an unrecognised language code resolves to.
DEFAULT_LANGUAGE = "it"
#: Every string is authored in English first, so it is the per-key fallback.
_SOURCE_LANGUAGE = "en"

LANGUAGE_NAMES = {"en": "English", "it": "Italiano"}

#: Must match ORIENTATIONS in tools/build_memes.py and Orientation in firmware/src/status.h.
ORIENTATIONS = ("landscape", "portrait")
DEFAULT_ORIENTATION = "portrait"

#: Must match kDisplayModeNames in firmware/src/status.cpp. Order matches the DisplayMode enum,
#: which is append-only so that a value already stored in the board's NVS keeps its meaning.
DISPLAY_MODES = ("image", "text", "mascot")
DEFAULT_DISPLAY_MODE = "mascot"

#: How the phrases are worded. Unlike everything else here this is a PC-side concept: the device
#: is told the tone only so the mascot can pull the matching face. Must match kToneNames in
#: firmware/src/status.cpp, and the folder names under captions/<lang>/.
TONES = ("normal", "sarcastic", "retriever")
DEFAULT_TONE = "normal"

_STATUS_LABELS: dict[str, dict[Status, str]] = {
    "en": {
        Status.AVAILABLE: "Available",
        Status.BUSY: "Busy",
        Status.IN_MEETING: "In a meeting",
        Status.DND: "Do not disturb",
        Status.AWAY: "Away",
        Status.BRB: "Be right back",
        Status.OFFLINE: "Offline",
        Status.UNKNOWN: "Unknown",
        Status.DISCONNECTED: "Disconnected",
    },
    "it": {
        Status.AVAILABLE: "Disponibile",
        Status.BUSY: "Occupato",
        Status.IN_MEETING: "In riunione",
        Status.DND: "Non disturbare",
        Status.AWAY: "Assente",
        Status.BRB: "Torno subito",
        Status.OFFLINE: "Non in linea",
        Status.UNKNOWN: "Sconosciuto",
        Status.DISCONNECTED: "Disconnesso",
    },
}

_MENU: dict[str, dict[str, str]] = {
    "en": {
        "next_meme": "Next meme",
        "force_status": "Force status",
        "follow_teams": "Follow Teams",
        "language": "Language",
        "orientation": "Orientation",
        "landscape": "Landscape",
        "portrait": "Portrait",
        "display": "Display",
        "image": "Image + text",
        "text": "Text only",
        "mascot": "Mascot",
        "tone": "Tone",
        "normal": "Normal",
        "sarcastic": "Sarcasm",
        "retriever": "Golden Retriever",
        "settings": "Messages and settings...",
        "transition": "Fade between captions",
        "reconnect": "Reconnect",
        "open_config": "Open config folder",
        "start_with_windows": "Start with Windows",
        "quit": "Quit",
        "not_connected": "not connected",
        "forced": "forced",
        "tooltip": "Teams status meme display",
    },
    "it": {
        "next_meme": "Prossimo meme",
        "force_status": "Forza stato",
        "follow_teams": "Segui Teams",
        "language": "Lingua",
        "orientation": "Orientamento",
        "landscape": "Orizzontale",
        "portrait": "Verticale",
        "display": "Visualizzazione",
        "image": "Immagine + testo",
        "text": "Solo testo",
        "mascot": "Mascotte",
        "tone": "Tono",
        "normal": "Normale",
        "sarcastic": "Sarcasmo",
        "retriever": "Golden Retriever",
        "settings": "Messaggi e impostazioni...",
        "transition": "Dissolvenza tra le frasi",
        "reconnect": "Riconnetti",
        "open_config": "Apri cartella configurazione",
        "start_with_windows": "Avvia con Windows",
        "quit": "Esci",
        "not_connected": "non connesso",
        "forced": "forzato",
        "tooltip": "Stato Teams con meme",
    },
}


def normalise(language: str | None) -> str:
    """Fall back to the default for anything we do not have strings for."""
    if language and language.lower() in LANGUAGES:
        return language.lower()
    return DEFAULT_LANGUAGE


def normalise_tone(tone: str | None) -> str:
    """Fall back to the default for a tone we have no phrases or face for."""
    if tone and tone.lower() in TONES:
        return tone.lower()
    return DEFAULT_TONE


def status_label(status: Status, language: str = DEFAULT_LANGUAGE) -> str:
    return _STATUS_LABELS[normalise(language)].get(status, str(status))


def tr(key: str, language: str = DEFAULT_LANGUAGE) -> str:
    table = _MENU[normalise(language)]
    # Two different fallbacks: an unknown *language* resolves to the default (normalise), but a
    # missing *key* falls back to English, which is the source language every string starts in.
    return table.get(key) or _MENU[_SOURCE_LANGUAGE].get(key, key)
