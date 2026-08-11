"""Behavioural tests for core/sequences.py — the behaviour/sequence surface.

The pure normalisers (text command, sequence alias, soundbank alias, skill id,
queue delay) and the catalog builders are tested directly.  The four live
operations are exercised against a fake ``AlexaAPI`` so device binding, the
``queue_delay``-omission contract and the returned rows are covered without a
live account.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import sequences
from cli_anything.alexa.core.device_ref import DeviceRef


def _run(coro):
    return asyncio.run(coro)


def _echo(serial="SN1", name="Kitchen", online=True):
    return {
        "serialNumber": serial,
        "accountName": name,
        "deviceType": "A3S5BH2HU6VAYF",
        "deviceFamily": "ECHO",
        "online": online,
    }


def _fake_api(devices, api_instance=None):
    """A stand-in ``AlexaAPI`` class: static get_devices + instance methods."""
    api = api_instance or MagicMock()
    for method in ("run_custom", "send_sequence", "run_skill", "play_sound"):
        setattr(api, method, AsyncMock(return_value=None))
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.return_value = api
    return fake_cls, api


# ── normalize_command_text ──────────────────────────────────────────────


def test_normalize_command_text_keeps_the_utterance_verbatim():
    """Amazon parses the text as spoken, so wording must not be rewritten."""
    assert (
        sequences.normalize_command_text("  turn off the kitchen lights  ")
        == "turn off the kitchen lights"
    )


@pytest.mark.parametrize("bad", [None, "", "   ", "\t\n"])
def test_normalize_command_text_rejects_empty(bad):
    with pytest.raises(ValueError, match="a command is required"):
        sequences.normalize_command_text(bad)


# ── normalize_sequence ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("weather", "Alexa.Weather.Play"),
        ("Weather", "Alexa.Weather.Play"),
        ("good night", "Alexa.GoodNight.Play"),
        ("good_night", "Alexa.GoodNight.Play"),
        ("GOOD-NIGHT", "Alexa.GoodNight.Play"),
        ("  flash briefing  ", "Alexa.FlashBriefing.Play"),
        ("calendar-tomorrow", "Alexa.Calendar.PlayTomorrow"),
    ],
)
def test_normalize_sequence_resolves_friendly_aliases(given, expected):
    assert sequences.normalize_sequence(given) == expected


def test_normalize_sequence_passes_through_raw_alexa_types():
    """Amazon keeps adding behaviours; an `Alexa.*` id must not need a release."""
    assert sequences.normalize_sequence("Alexa.Something.Brand.New") == "Alexa.Something.Brand.New"


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_normalize_sequence_rejects_empty_and_lists_options(bad):
    with pytest.raises(ValueError, match="a sequence is required"):
        sequences.normalize_sequence(bad)


def test_normalize_sequence_rejects_unknown_alias_with_alternatives():
    with pytest.raises(ValueError) as err:
        sequences.normalize_sequence("make-tea")
    message = str(err.value)
    assert "unknown sequence" in message
    assert "weather" in message  # the alternatives are listed


# ── normalize_sound ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("doorbell", "amzn_sfx_doorbell_chime_01"),
        ("Air Horn", "air_horn_03"),
        ("air_horn", "air_horn_03"),
        ("  ZAP ", "zap_01"),
    ],
)
def test_normalize_sound_resolves_aliases(given, expected):
    assert sequences.normalize_sound(given) == expected


@pytest.mark.parametrize(
    "raw_id",
    ["amzn_sfx_cat_meow_1x_02", "bell_02", "clock_ticking_01", "AMZN_SFX_DOG_MED_BARK_1X_02"],
)
def test_normalize_sound_accepts_raw_soundbank_ids_verbatim(raw_id):
    """The soundbank is Amazon's and grows — a raw id is lower-cased and sent."""
    assert sequences.normalize_sound(raw_id) == raw_id.lower()


@pytest.mark.parametrize("bad", [None, "", "  "])
def test_normalize_sound_rejects_empty(bad):
    with pytest.raises(ValueError, match="a sound is required"):
        sequences.normalize_sound(bad)


@pytest.mark.parametrize("bad", ["not a sound!", "9lives", "sound with a space", "x"])
def test_normalize_sound_rejects_unknown_alias_shapes(bad):
    with pytest.raises(ValueError, match="unknown sound"):
        sequences.normalize_sound(bad)


def test_normalize_sound_treats_a_bare_word_as_a_raw_id_not_an_alias_typo():
    """`trumpets` is *not* the `trumpet` alias — it is a valid raw-id shape, so it
    is forwarded verbatim (Amazon owns the catalog) rather than second-guessed."""
    assert sequences.normalize_sound("trumpets") == "trumpets"
    assert sequences.normalize_sound("trumpet") == "amzn_sfx_trumpet_bugle_04"


# ── normalize_skill_id ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "skill_id",
    [
        "amzn1.ask.skill.7b8a9c0d-1e2f-3a4b-5c6d-7e8f9a0b1c2d",
        "amzn1.ask.1p.tellalexa",
        "amzn1.ask.1p.music",
    ],
)
def test_normalize_skill_id_accepts_amazon_shapes(skill_id):
    assert sequences.normalize_skill_id(f"  {skill_id} ") == skill_id


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_normalize_skill_id_rejects_empty(bad):
    with pytest.raises(ValueError, match="a skill id is required"):
        sequences.normalize_skill_id(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "skill.1234",
        "amzn1.ask.other.1234",
        "amzn1.alexa.endpoint.abc",
        "amzn1.ask.skill",
        "Kitchen Skill",
    ],
)
def test_normalize_skill_id_rejects_non_skill_ids(bad):
    """A bad id fails silently on the device, so it is refused where we can explain."""
    with pytest.raises(ValueError, match="is not an Alexa skill id"):
        sequences.normalize_skill_id(bad)


# ── normalize_queue_delay ───────────────────────────────────────────────


@pytest.mark.parametrize("unspecified", [None, ""])
def test_normalize_queue_delay_unspecified_is_none(unspecified):
    """None means "omit the argument" so alexapy's per-call default survives."""
    assert sequences.normalize_queue_delay(unspecified) is None


@pytest.mark.parametrize(("given", "expected"), [(0, 0.0), ("1.5", 1.5), (3, 3.0), (0.25, 0.25)])
def test_normalize_queue_delay_accepts_non_negative_numbers(given, expected):
    assert sequences.normalize_queue_delay(given) == pytest.approx(expected)


@pytest.mark.parametrize("bad", ["soon", object(), "1.2.3"])
def test_normalize_queue_delay_rejects_non_numbers(bad):
    with pytest.raises(ValueError, match="must be a number of seconds"):
        sequences.normalize_queue_delay(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_normalize_queue_delay_rejects_nan_and_inf(bad):
    """NaN/inf slip past a naive `< 0` check and poison the payload."""
    with pytest.raises(ValueError, match="must be a number of seconds"):
        sequences.normalize_queue_delay(bad)


@pytest.mark.parametrize("bad", [-1, -0.5, "-2"])
def test_normalize_queue_delay_rejects_negative(bad):
    with pytest.raises(ValueError, match="must not be negative"):
        sequences.normalize_queue_delay(bad)


# ── catalog ─────────────────────────────────────────────────────────────


def test_sequence_rows_are_sorted_and_carry_the_alexa_type():
    rows = sequences.sequence_rows()
    assert [r["name"] for r in rows] == sorted(sequences.SEQUENCE_COMMANDS)
    assert {"name", "sequence"} == set(rows[0])
    assert all(r["sequence"].startswith("Alexa.") for r in rows)


def test_sound_rows_are_sorted_and_carry_the_soundbank_id():
    rows = sequences.sound_rows()
    assert [r["name"] for r in rows] == sorted(sequences.SOUND_ALIASES)
    assert {"name", "sound"} == set(rows[0])


@pytest.mark.parametrize("kind", [None, "", "all", "ALL", " all "])
def test_catalog_defaults_to_both_kinds(kind):
    data = sequences.catalog(kind)
    assert set(data) == {"sequences", "sounds"}


@pytest.mark.parametrize("kind", ["sequence", "sequences", "SEQUENCES"])
def test_catalog_sequences_only(kind):
    assert set(sequences.catalog(kind)) == {"sequences"}


@pytest.mark.parametrize("kind", ["sound", "sounds"])
def test_catalog_sounds_only(kind):
    assert set(sequences.catalog(kind)) == {"sounds"}


def test_catalog_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown catalog"):
        sequences.catalog("skills")


# ── live operations ─────────────────────────────────────────────────────


def test_run_command_sends_the_utterance_to_the_first_online_echo():
    fake_cls, api = _fake_api([_echo("SN0", "Study", online=False), _echo("SN1", "Kitchen")])
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(sequences.run_command(MagicMock(), None, " tell me a joke "))
    api.run_custom.assert_awaited_once_with("tell me a joke")
    assert row == {"device": "Kitchen", "command": "tell me a joke", "ok": True}
    # bound through DeviceRef, never the raw dict alexapy would choke on
    assert isinstance(fake_cls.call_args.args[0], DeviceRef)


def test_run_command_passes_queue_delay_when_given():
    fake_cls, api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        _run(sequences.run_command(MagicMock(), "Kitchen", "good morning", queue_delay="2"))
    api.run_custom.assert_awaited_once_with("good morning", queue_delay=2.0)


def test_run_command_validates_before_touching_the_network():
    fake_cls, _api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="a command is"):
        _run(sequences.run_command(MagicMock(), "Kitchen", "   "))
    fake_cls.get_devices.assert_not_awaited()


def test_run_sequence_resolves_the_alias_and_omits_the_default_delay():
    fake_cls, api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(sequences.run_sequence(MagicMock(), "Kitchen", "good-night"))
    api.send_sequence.assert_awaited_once_with("Alexa.GoodNight.Play")
    assert row == {"device": "Kitchen", "sequence": "Alexa.GoodNight.Play", "ok": True}


def test_run_sequence_with_explicit_delay():
    fake_cls, api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        _run(sequences.run_sequence(MagicMock(), None, "weather", queue_delay=0))
    api.send_sequence.assert_awaited_once_with("Alexa.Weather.Play", queue_delay=0.0)


def test_run_skill_launches_by_id():
    fake_cls, api = _fake_api([_echo()])
    skill = "amzn1.ask.skill.7b8a9c0d-1e2f-3a4b-5c6d-7e8f9a0b1c2d"
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(sequences.run_skill(MagicMock(), "Kitchen", skill, queue_delay=1.5))
    api.run_skill.assert_awaited_once_with(skill, queue_delay=1.5)
    assert row == {"device": "Kitchen", "skill": skill, "ok": True}


def test_run_skill_rejects_a_bad_id_before_resolving_a_device():
    fake_cls, _api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="not an Alexa skill"):
        _run(sequences.run_skill(MagicMock(), None, "nope"))
    fake_cls.get_devices.assert_not_awaited()


def test_play_sound_resolves_the_alias():
    fake_cls, api = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(sequences.play_sound(MagicMock(), "Kitchen", "doorbell"))
    api.play_sound.assert_awaited_once_with("amzn_sfx_doorbell_chime_01")
    assert row == {"device": "Kitchen", "sound": "amzn_sfx_doorbell_chime_01", "ok": True}


def test_play_sound_unknown_device_is_a_value_error():
    fake_cls, api = _fake_api([_echo("SN1", "Kitchen")])
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="no device matching"):
        _run(sequences.play_sound(MagicMock(), "Garage", "bell"))
    api.play_sound.assert_not_awaited()


def test_text_command_skill_id_is_recorded_for_documentation():
    """alexapy's run_custom rides on this first-party skill; keep the id pinned."""
    assert sequences.TEXT_COMMAND_SKILL_ID == "amzn1.ask.1p.tellalexa"
    assert sequences.normalize_skill_id(sequences.TEXT_COMMAND_SKILL_ID)
