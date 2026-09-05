"""Tests for the tone axis, the PC-side phrase bank, and the generated mascot table.

The theme running through these: the PC now owns what the device says. That makes the phrase
bank, its fallbacks and the status/tone contract worth pinning down, because a mistake here shows
up as the wrong words on somebody's desk rather than as an exception.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_memes  # noqa: E402
import gen_mascot_table  # noqa: E402
from pc_app.config import Config  # noqa: E402
from pc_app.i18n import DEFAULT_TONE, LANGUAGES, TONES, normalise_tone, tr  # noqa: E402
from pc_app.mascot_faces import BASE_FACES, IDLES, STATUS_NAMES, TONE_MODIFIERS, face_for  # noqa: E402
from pc_app.phrases import MAX_PHRASE_CHARS, PhraseBank  # noqa: E402
from pc_app.presence import Status  # noqa: E402
from pc_app.text import to_display_ascii  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


# -- the tone axis -------------------------------------------------------------------------


def test_tone_names_are_translated_in_every_language():
    for language in LANGUAGES:
        for tone in TONES:
            assert tr(tone, language) != tone, (language, tone)


def test_unknown_tone_falls_back_to_the_default():
    assert normalise_tone("SARCASTIC") == "sarcastic"
    assert normalise_tone("enthusiastic") == DEFAULT_TONE
    assert normalise_tone(None) == DEFAULT_TONE


def test_config_defaults_to_a_valid_tone_and_mode():
    config = Config()
    assert config.tone in TONES
    assert config.display_mode == "mascot"


def test_config_written_before_tones_existed_still_loads(tmp_path):
    """A config file from 1.2 has no tone and asks for image mode; both must survive."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"language": "it", "display_mode": "image", "brightness": 55}),
        encoding="utf-8",
    )
    config = Config.load(path)
    assert config.display_mode == "image"  # the user's explicit choice is not overridden
    assert config.brightness == 55
    assert config.tone == DEFAULT_TONE  # and the new setting takes its default


# -- the phrase bank -----------------------------------------------------------------------


def test_seeds_every_combination_from_the_shipped_banks():
    bank = PhraseBank()
    for language in LANGUAGES:
        for tone in TONES:
            for status in Status:
                lines = bank.lines(language, tone, status)
                assert lines, (language, tone, status)


def test_every_shipped_phrase_fits_the_wire_and_the_font():
    """A phrase longer than the cap is truncated mid-sentence on the device."""
    bank = PhraseBank()
    for language in LANGUAGES:
        for tone in TONES:
            for status in Status:
                for line in bank.lines(language, tone, status):
                    folded, lost = to_display_ascii(line)
                    assert not lost, (language, tone, status, line, lost)
                    assert len(folded) <= MAX_PHRASE_CHARS, (line, len(folded))


def test_pick_does_not_repeat_a_phrase_back_to_back():
    bank = PhraseBank()
    picks = [bank.pick(Status.BUSY, "sarcastic", "en") for _ in range(30)]
    assert all(a != b for a, b in zip(picks, picks[1:]))


def test_pick_of_a_single_phrase_is_not_an_infinite_search():
    bank = PhraseBank()
    bank.set_lines("en", "normal", Status.BUSY, ["the only one"])
    assert bank.pick(Status.BUSY, "normal", "en") == "the only one"
    assert bank.pick(Status.BUSY, "normal", "en") == "the only one"


def test_an_empty_tone_falls_back_to_normal():
    bank = PhraseBank()
    bank.set_lines("en", "sarcastic", Status.DND, [])
    assert bank.pick(Status.DND, "sarcastic", "en") in bank.lines("en", "normal", Status.DND)


def test_an_empty_bank_falls_back_to_the_status_label():
    """Never an empty caption band: an honest word beats a blank."""
    empty = {
        language: {tone: {status.value.lower(): [] for status in Status} for tone in TONES}
        for language in LANGUAGES
    }
    bank = PhraseBank(empty)
    assert bank.pick(Status.IN_MEETING, "retriever", "en") == "In a meeting"
    assert bank.pick(Status.IN_MEETING, "retriever", "it") == "In riunione"


def test_unknown_language_and_tone_are_normalised_rather_than_raising():
    bank = PhraseBank()
    assert bank.pick(Status.AWAY, "enthusiastic", "de")


def test_edits_round_trip_and_a_deliberate_emptying_survives(tmp_path):
    """Loading layers the file over the shipped banks, so new content appears in untouched
    combinations without resurrecting a list the user cleared on purpose."""
    path = tmp_path / "phrases.json"
    bank = PhraseBank()
    bank.set_lines("it", "retriever", Status.DND, ["Solo per te.", "Sempre."])
    bank.set_lines("en", "sarcastic", Status.BUSY, [])
    bank.save(path)

    again = PhraseBank.load(path)
    assert again.lines("it", "retriever", Status.DND) == ["Solo per te.", "Sempre."]
    assert again.lines("en", "sarcastic", Status.BUSY) == []
    assert again.lines("en", "retriever", Status.AVAILABLE)  # untouched: still seeded


def test_a_corrupt_phrase_file_does_not_lose_the_shipped_banks(tmp_path):
    path = tmp_path / "phrases.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert PhraseBank.load(path).lines("en", "normal", Status.BUSY)


def test_set_lines_drops_blanks_and_strips():
    bank = PhraseBank()
    bank.set_lines("en", "normal", Status.BUSY, ["  padded  ", "", "   ", "second"])
    assert bank.lines("en", "normal", Status.BUSY) == ["padded", "second"]


def test_counts_cover_every_status():
    counts = PhraseBank().counts("en", "normal")
    assert set(counts) == {status.value.lower() for status in Status}
    assert all(count > 0 for count in counts.values())


# -- the contract between the three sides ---------------------------------------------------


def test_tones_match_the_caption_folders_on_disk():
    for language in LANGUAGES:
        found = {p.name for p in (REPO / "captions" / language).iterdir() if p.is_dir()}
        assert found == set(TONES), (language, found)


def test_the_flashed_tone_is_one_of_the_tones():
    assert build_memes.FLASHED_TONE in TONES


# -- the mascot table -----------------------------------------------------------------------


def test_the_generated_firmware_table_is_up_to_date():
    """The firmware and the GUI preview read the same expressions; this is what keeps them so."""
    assert gen_mascot_table.main(["--check"]) == 0, (
        "firmware/src/mascot_table.h is stale -- run: python tools/gen_mascot_table.py"
    )


def test_the_table_covers_every_status_and_tone():
    assert set(STATUS_NAMES) == set(build_memes.STATUSES)
    assert set(BASE_FACES) == set(STATUS_NAMES)
    assert set(TONE_MODIFIERS) == set(TONES)
    assert len(list(_all_faces())) == len(STATUS_NAMES) * len(TONES)


def _all_faces():
    for status in STATUS_NAMES:
        for tone in TONES:
            yield status, tone, face_for(status, tone)


@pytest.mark.parametrize("status", STATUS_NAMES)
def test_sarcasm_reads_as_unimpressed_whatever_the_status(status):
    """The point of the mode: green on the badge, unenthusiastic on the face."""
    face = face_for(status, "sarcastic")
    assert face.eye_open <= 45, status
    assert face.mouth_curve <= 0, status
    assert face.brow_asym, status
    assert not face.blush, status


@pytest.mark.parametrize("status", STATUS_NAMES)
def test_the_retriever_is_delighted_whatever_the_status(status):
    face = face_for(status, "retriever")
    assert face.mouth_curve >= 80, status
    assert face.eye_open == 100, status
    assert face.blush, status
    assert face.idle == "bounce", status


def test_normal_leaves_the_status_face_alone():
    for status in STATUS_NAMES:
        assert face_for(status, "normal") == BASE_FACES[status]


def test_every_face_is_in_range_and_uses_a_known_idle():
    for status, tone, face in _all_faces():
        where = (status, tone)
        assert -100 <= face.brow_tilt <= 100, where
        assert 0 <= face.eye_open <= 100, where
        assert -100 <= face.mouth_curve <= 100, where
        assert 0 <= face.mouth_open <= 100, where
        assert face.idle in IDLES, where


def test_the_sleeping_statuses_stay_asleep_under_sarcasm():
    """Sarcasm clamps the eyes rather than setting them, so it cannot wake a shut face."""
    for status in ("away", "offline"):
        assert face_for(status, "sarcastic").eye_open == BASE_FACES[status].eye_open
