"""Text handling shared by the tray app, the GUI and the meme pack builder.

The device's font is ASCII only and its caption band is a fixed number of lines, so a phrase has
to survive two separate reductions before it reaches the screen: characters it cannot draw, and
width it cannot fit. Both live here so the GUI can warn about them while you type, rather than
letting you discover the truncation on the panel.
"""

from __future__ import annotations

import unicodedata

#: The display font is ASCII only (TFT_eSPI font 2 carries 96 glyphs, 32-126), so accented
#: letters are folded to the apostrophe form Italian has always used on ASCII-only systems.
ACCENT_MAP = {
    "à": "a'", "è": "e'", "é": "e'", "ì": "i'", "ò": "o'", "ó": "o'", "ù": "u'",
    "À": "A'", "È": "E'", "É": "E'", "Ì": "I'", "Ò": "O'", "Ó": "O'", "Ù": "U'",
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...",
}


def to_display_ascii(text: str) -> tuple[str, list[str]]:
    """Fold text to what the display font can actually render.

    Returns the folded text plus any characters that had to be dropped, so callers can warn
    rather than silently printing question marks on the device.
    """
    out: list[str] = []
    lost: list[str] = []
    for char in text:
        if ord(char) < 128:
            out.append(char)
        elif char in ACCENT_MAP:
            out.append(ACCENT_MAP[char])
        else:
            # Last resort: strip the diacritic (é -> e) if that yields something printable.
            stripped = "".join(
                c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c)
            )
            if stripped and all(ord(c) < 128 for c in stripped):
                out.append(stripped)
            else:
                lost.append(char)
                out.append("?")
    return "".join(out), lost


def wrap_caption(text: str, measure, width: int, max_lines: int, pad_x: int) -> list[str]:
    """Greedy word wrap by measured pixel width, mirroring wrapText() in display.cpp.

    Width rather than character count: TFT_eSPI font 2 is proportional, so counting characters
    would wrap in the wrong place. The preview font is not byte-identical to the device font, so
    a line may break one word differently -- close enough to judge layout, not a pixel contract.
    """
    max_width = width - 2 * pad_x
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if measure(candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        # A single word too wide for the line is hard-broken rather than left to overflow.
        while measure(word) > max_width and len(word) > 1:
            fit = len(word)
            while fit > 1 and measure(word[:fit]) > max_width:
                fit -= 1
            lines.append(word[:fit])
            word = word[fit:]
        current = word
    if current:
        lines.append(current)
    return lines[:max_lines]
