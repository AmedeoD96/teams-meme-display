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


def test_every_language_has_a_caption_bank_for_every_status():
    root = Path(__file__).resolve().parents[2] / "captions"
    for language in LANGUAGES:
        for status in build_memes.STATUSES:
            path = root / language / f"{status}.txt"
            assert path.exists(), path
            lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            assert lines, path


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
        for path in sorted((root / language).glob("*.txt")):
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

    assert DISPLAY_MODES == ("image", "text")
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
