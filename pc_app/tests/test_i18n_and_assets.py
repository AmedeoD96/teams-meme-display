"""Tests for the language / orientation options and the asset-pipeline rules they rely on."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_memes  # noqa: E402
from pc_app.config import Config  # noqa: E402
from pc_app.i18n import (  # noqa: E402
    LANGUAGES,
    ORIENTATIONS,
    normalise,
    status_label,
    tr,
)
from pc_app.presence import STATUS_COLOR, Status  # noqa: E402


# -- language selection ------------------------------------------------------------------


def test_every_status_has_a_label_in_every_language():
    for language in LANGUAGES:
        for status in Status:
            label = status_label(status, language)
            assert label and label != str(status), (language, status)


def test_italian_and_english_labels_actually_differ():
    """Guards against a half-finished translation silently shipping English."""
    same = [s for s in Status if status_label(s, "en") == status_label(s, "it")]
    # OFFLINE is the only word we deliberately translate to a different string, so nothing
    # should collide; if a future status legitimately matches, add it here on purpose.
    assert same == [], same


def test_unknown_language_falls_back_to_the_default():
    from pc_app.i18n import DEFAULT_LANGUAGE

    assert normalise("de") == DEFAULT_LANGUAGE
    assert normalise(None) == DEFAULT_LANGUAGE
    assert normalise("IT") == "it"
    assert normalise("EN") == "en"
    assert status_label(Status.BUSY, "de") == status_label(Status.BUSY, DEFAULT_LANGUAGE)
    assert tr("quit", "klingon") == tr("quit", DEFAULT_LANGUAGE)


def test_menu_keys_are_translated_in_every_language():
    keys = ("next_meme", "force_status", "language", "orientation", "quit", "not_connected")
    for language in LANGUAGES:
        for key in keys:
            assert tr(key, language) != key, (language, key)


def test_config_defaults_are_valid_choices():
    config = Config()
    assert config.language in LANGUAGES
    assert config.orientation in ORIENTATIONS


# -- the contract between the three sides -------------------------------------------------


def test_pc_and_build_tool_agree_on_languages_and_statuses():
    assert tuple(LANGUAGES) == tuple(build_memes.LANGUAGES)
    assert {s.value.lower() for s in Status} == set(build_memes.STATUSES)


def test_pc_and_build_tool_agree_on_status_colours():
    """The tray icon and the device fallback scene should be the same colour."""
    for status in Status:
        expected = tuple(int(STATUS_COLOR[status].lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
        assert build_memes.status_colour(status.value.lower()) == expected, status


def test_orientation_names_map_onto_the_device_folders():
    for name in ORIENTATIONS:
        assert build_memes.ORIENTATION_ALIASES[name] in build_memes.ORIENTATIONS


def test_every_language_and_tone_has_a_caption_bank_for_every_status():
    """Every combination the GUI can select must start with something in it."""
    from pc_app.i18n import TONES

    root = Path(__file__).resolve().parents[2] / "captions"
    for language in LANGUAGES:
        for tone in TONES:
            for status in build_memes.STATUSES:
                path = root / language / tone / f"{status}.txt"
                assert path.exists(), path
                lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
                assert lines, path


def test_only_the_normal_tone_is_flashed_to_the_device():
    """Tones are a PC-side concept; the board only carries the offline fallback bank."""
    assert build_memes.FLASHED_TONE == "normal"


# -- ASCII folding for the display font ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("finché dura", "finche' dura"),
        ("Caffè fatto", "Caffe' fatto"),
        ("È successo", "E' successo"),
        ("Non durerà.", "Non durera'."),
        ("più", "piu'"),
        ("plain ascii", "plain ascii"),
    ],
)
def test_italian_accents_fold_to_the_apostrophe_form(raw, expected):
    """The display font is ASCII only, so accents become the ASCII form Italians already use."""
    folded, lost = build_memes.to_display_ascii(raw)
    assert folded == expected
    assert lost == []


def test_unrenderable_characters_are_reported_not_hidden():
    folded, lost = build_memes.to_display_ascii("emoji \U0001F600 here")
    assert "?" in folded
    assert lost == ["\U0001F600"]


def test_all_built_captions_are_display_safe():
    """Every shipped caption must survive folding without losing a character."""
    root = Path(__file__).resolve().parents[2] / "captions"
    for language in LANGUAGES:
        for path in sorted((root / language).glob("*/*.txt")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                folded, lost = build_memes.to_display_ascii(line)
                assert not lost, f"{path.name}: {line!r} loses {lost}"
                assert all(ord(c) < 127 for c in folded), f"{path.name}: {folded!r}"


# -- per-orientation source images --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("meme.png", None),
        ("meme.land.png", "land"),
        ("meme.port.png", "port"),
        ("meme.landscape.jpg", "land"),
        ("meme.portrait.jpg", "port"),
        ("00_sample.land.png", "land"),
        # A dot in the name that is not an orientation must not be mistaken for one.
        ("my.favourite.meme.png", None),
        ("v1.2.png", None),
    ],
)
def test_orientation_suffix_convention(name, expected):
    assert build_memes.source_orientation(Path(name)) is expected


def test_source_selection_respects_the_suffix(tmp_path, monkeypatch):
    folder = tmp_path / "busy"
    folder.mkdir()
    for name in ("both.png", "only.land.png", "only.port.png"):
        (folder / name).write_bytes(b"")
    monkeypatch.setattr(build_memes, "MEME_SRC", tmp_path)

    land = {p.name for p in build_memes.source_images("busy", "land")}
    port = {p.name for p in build_memes.source_images("busy", "port")}
    assert land == {"both.png", "only.land.png"}
    assert port == {"both.png", "only.port.png"}


def test_caption_wrapping_is_narrower_in_portrait():
    """Sanity check that the wrap helper honours the frame width it is given."""
    text = "Do not disturb. Compiling. Do not perceive me."
    measure = len  # 1 unit per character keeps the assertion about width, not fonts
    wide = build_memes.wrap_caption(text, measure, 320)
    narrow = build_memes.wrap_caption(text, measure, 240)
    assert len(narrow) >= len(wide)
    assert all(len(line) <= 320 for line in wide)


# -- display mode ---------------------------------------------------------------------------


def test_display_modes_are_translated_and_defaulted():
    from pc_app.i18n import DISPLAY_MODES

    assert DISPLAY_MODES == ("image", "text", "mascot")
    for language in LANGUAGES:
        for mode in DISPLAY_MODES:
            assert tr(mode, language) != mode, (language, mode)
    assert Config().display_mode in DISPLAY_MODES


def test_defaults_are_italian_portrait():
    """The user's chosen defaults; the firmware NVS fallbacks must match these."""
    config = Config()
    assert config.language == "it"
    assert config.orientation == "portrait"


def test_transition_default_is_a_visible_but_short_fade():
    ms = Config().transition_ms
    assert 0 < ms <= 1000, ms


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # Light themes need black text; dark ones need white.
        ("available", (0, 0, 0)),
        ("away", (0, 0, 0)),
        ("brb", (0, 0, 0)),
        ("in_meeting", (255, 255, 255)),
        ("dnd", (255, 255, 255)),
        ("disconnected", (255, 255, 255)),
        ("unknown", (255, 255, 255)),
    ],
)
def test_text_mode_picks_a_readable_text_colour(status, expected):
    assert build_memes.contrast_on(build_memes.status_colour(status)) == expected


def test_text_mode_labels_cover_every_status_and_language():
    for language in LANGUAGES:
        for status in build_memes.STATUSES:
            assert build_memes.STATUS_LABELS[language][status]


def test_text_mode_allows_more_lines_than_the_caption_band():
    """Text mode has the whole screen, so a long caption should not be truncated as hard."""
    assert build_memes.TEXT_MAX_LINES > build_memes.CAPTION_MAX_LINES
    # ~12px per character, so a 240px-wide screen fits about 19 -- close enough to the real font
    # for the line count to be meaningful. Plain len() would pretend a pixel is a character.
    measure = lambda text: len(text) * 12  # noqa: E731
    long_caption = "parola " * 30
    banded = build_memes.wrap_caption(long_caption, measure, 240)
    full = build_memes.wrap_caption(long_caption, measure, 240, max_lines=build_memes.TEXT_MAX_LINES)
    assert len(banded) == build_memes.CAPTION_MAX_LINES
    assert len(full) > len(banded)


def test_config_written_by_an_older_version_still_loads(tmp_path):
    """Adding settings must not break a config file that predates them."""
    import json

    from pc_app.i18n import DISPLAY_MODES

    old = tmp_path / "config.json"
    old.write_text(
        json.dumps({"language": "it", "orientation": "portrait", "brightness": 55}),
        encoding="utf-8",
    )
    config = Config.load(old)

    assert config.language == "it"
    assert config.brightness == 55
    # The settings the old file knew nothing about come from the defaults.
    assert config.display_mode in DISPLAY_MODES
    assert config.transition_ms == Config().transition_ms


def test_config_with_a_setting_we_removed_still_loads(tmp_path):
    import json

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"language": "en", "a_setting_from_the_future": 42}), encoding="utf-8")
    config = Config.load(path)
    assert config.language == "en"
    assert not hasattr(config, "a_setting_from_the_future")


# -- the caption band ------------------------------------------------------------------------


def test_a_short_caption_gets_the_big_font():
    from pc_app.render import CAPTION_FONT_BIG, layout_caption

    lines, size, _, truncated = layout_caption("Sono libero.", 240)
    assert size == CAPTION_FONT_BIG
    assert not truncated
    assert lines == ["Sono libero."]


def test_a_long_caption_drops_to_the_small_font_rather_than_being_cut():
    """The band trades size for completeness; losing half a joke is worse than losing 10px."""
    from pc_app.render import CAPTION_FONT_SMALL, CAPTION_LINES_BIG, layout_caption

    # The longest phrase we ship. At the big font it would need more than the three lines the
    # band has room for, so the band steps down a size instead of losing the punchline.
    long_one = "Inattivo. Come il server di build. Come le mie speranze."
    lines, size, _, truncated = layout_caption(long_one, 240)
    assert size == CAPTION_FONT_SMALL, "should have fallen back rather than truncating"
    assert not truncated
    assert " ".join(lines) == long_one, "no words lost on the way down to the small font"

    # And it really was too tall for the big font -- otherwise this test proves nothing.
    from pc_app.render import CAPTION_FONT_BIG, preview_font, wrap_caption
    from PIL import Image, ImageDraw

    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    big = preview_font(CAPTION_FONT_BIG)
    at_big = wrap_caption(
        long_one, lambda t: scratch.textlength(t, font=big), 240, max_lines=99
    )
    assert len(at_big) > CAPTION_LINES_BIG


def test_every_shipped_phrase_fits_the_band_without_being_cut():
    """The real bar: nothing we ship should reach the panel half-said, in either orientation."""
    from pc_app.phrases import PhraseBank
    from pc_app.render import layout_caption
    from pc_app.text import to_display_ascii

    bank = PhraseBank()
    for width in (240, 320):
        for language in LANGUAGES:
            for tone in ("normal", "sarcastic", "retriever"):
                for status in build_memes.STATUSES:
                    for line in bank.lines(language, tone, status):
                        folded, _ = to_display_ascii(line)
                        _, _, _, truncated = layout_caption(folded, width)
                        assert not truncated, (width, language, tone, status, line)


def test_a_shorter_caption_sits_lower_than_a_taller_one():
    """The condition behind the overlap bug: the band is bottom-aligned, so a caption needing
    fewer lines starts further down and cannot cover what the previous one drew above it.
    Clearing only the new band is what left a stale line on screen."""
    from pc_app.render import CAPTION_PAD_Y, layout_caption

    def band_top(caption, height=320, width=240):
        lines, _, line_h, _ = layout_caption(caption, width)
        return height - (len(lines) * line_h + 2 * CAPTION_PAD_Y)

    two = band_top("Tutti i test passano. Non toccare niente.")
    one = band_top("Sono libero.")
    assert one > two, "a one-line band must start below a two-line one"


def test_the_mascot_never_overlaps_the_caption_band():
    from pc_app.render import CAPTION_RESERVE, mascot_box

    for size in ((240, 320), (320, 240)):
        _, top, side = mascot_box(size)
        assert top + side <= size[1] - CAPTION_RESERVE, size


def test_the_mascot_leaves_most_of_the_panel_to_everything_else():
    """It was shrunk deliberately; this stops it creeping back up."""
    from pc_app.render import MASCOT_MAX_SIZE, mascot_box

    assert MASCOT_MAX_SIZE == 130
    _, _, side = mascot_box((240, 320))
    assert side <= MASCOT_MAX_SIZE


# -- the text-mode presence badge --------------------------------------------------------------


def _badge_crop(status: str):
    from pc_app.render import TEXT_BADGE_CY, TEXT_BADGE_R, render_text_frame

    frame = render_text_frame(status, "caption", (240, 320))
    r = TEXT_BADGE_R + 2
    cx = 240 // 2
    return frame.crop((cx - r, TEXT_BADGE_CY - r, cx + r, TEXT_BADGE_CY + r)).tobytes()


def test_every_status_draws_its_own_badge():
    """Each status must be tellable from the badge alone -- that is the point of replacing the
    name with a glyph. A status falling through to the wrong branch would collide here."""
    crops = {status: _badge_crop(status) for status in build_memes.STATUSES}
    collisions = [
        (a, b)
        for i, a in enumerate(build_memes.STATUSES)
        for b in build_memes.STATUSES[i + 1 :]
        if crops[a] == crops[b]
    ]
    assert collisions == [], collisions


def test_the_badge_is_not_invisible_against_its_own_background():
    """The disc takes the caption colour rather than the status colour, because the background
    already *is* the status colour."""
    from pc_app.render import contrast_on, status_colour

    for status in build_memes.STATUSES:
        background = status_colour(status)
        assert contrast_on(background) != background


def test_the_badge_clears_the_caption_block():
    from pc_app.render import TEXT_BADGE_CY, TEXT_BADGE_R, TEXT_RULE_Y, TEXT_TOP

    assert TEXT_BADGE_CY - TEXT_BADGE_R >= 0, "badge runs off the top of the panel"
    assert TEXT_BADGE_CY + TEXT_BADGE_R < TEXT_RULE_Y, "badge overlaps its own rule"
    assert TEXT_RULE_Y < TEXT_TOP, "rule overlaps the caption"
