"""Behavioural tests for core/media.py — Echo transport, volume and player state.

Pure helpers (volume conversion, provider normalisation, player-state
flattening) are tested directly.  The live operations are exercised against a
fake ``AlexaAPI`` so the branching — device resolution, the 0-100 -> 0.0-1.0
conversion, verb dispatch, ``--all`` stop — is covered without a live account.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import media
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
    api = api_instance or MagicMock()
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.return_value = api
    return fake_cls, api


# ── normalize_volume ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [(0, 0.0), (50, 0.5), (100, 1.0), (33, 0.33), ("75", 0.75), (12.5, 0.125)],
)
def test_normalize_volume_converts_percentage_to_fraction(given, expected):
    """alexapy multiplies by 100, so it wants 0.0-1.0 — not the raw percentage."""
    assert media.normalize_volume(given) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [-1, 101, 1000, -0.5])
def test_normalize_volume_rejects_out_of_range(bad):
    with pytest.raises(ValueError, match="between 0 and 100"):
        media.normalize_volume(bad)


@pytest.mark.parametrize("bad", ["loud", None, "", object()])
def test_normalize_volume_rejects_non_numbers(bad):
    with pytest.raises(ValueError, match="must be a number"):
        media.normalize_volume(bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_normalize_volume_rejects_nan_and_infinity(bad):
    """NaN/inf slip past a naive range check and would be sent to Amazon."""
    with pytest.raises(ValueError):
        media.normalize_volume(bad)


def test_normalize_volume_error_names_the_bad_value():
    with pytest.raises(ValueError, match="200"):
        media.normalize_volume(200)


# ── normalize_provider ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("spotify", "SPOTIFY"),
        ("Amazon Music", "AMAZON_MUSIC"),
        ("apple-music", "APPLE_MUSIC"),
        ("  tunein  ", "TUNEIN"),
        ("FUTURE_PROVIDER", "FUTURE_PROVIDER"),
    ],
)
def test_normalize_provider_upper_snake_cases(given, expected):
    assert media.normalize_provider(given) == expected


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_normalize_provider_defaults_to_amazon_music(blank):
    assert media.normalize_provider(blank) == media.DEFAULT_MUSIC_PROVIDER


def test_default_provider_is_in_the_known_list():
    assert media.DEFAULT_MUSIC_PROVIDER in media.KNOWN_MUSIC_PROVIDERS


# ── player_row ──────────────────────────────────────────────────────────


def _playing_state():
    return {
        "playerInfo": {
            "state": "PLAYING",
            "infoText": {"title": "Take Five", "subText1": "Dave Brubeck", "subText2": "Time Out"},
            "volume": {"volume": 40, "muted": False},
            "provider": {"providerName": "Amazon Music"},
            "progress": {"mediaProgress": 61, "mediaLength": 324},
        }
    }


def test_player_row_flattens_the_nested_payload():
    row = media.player_row(_playing_state(), device="Kitchen")
    assert row["device"] == "Kitchen"
    assert row["state"] == "PLAYING"
    assert row["title"] == "Take Five"
    assert row["artist"] == "Dave Brubeck"
    assert row["album"] == "Time Out"
    assert row["provider"] == "Amazon Music"
    assert row["volume"] == 40
    assert row["muted"] is False
    assert row["progress_seconds"] == 61
    assert row["duration_seconds"] == 324


@pytest.mark.parametrize("empty", [None, {}, {"playerInfo": None}, {"playerInfo": {}}])
def test_player_row_handles_an_idle_speaker(empty):
    """An idle Echo returns an empty/absent playerInfo — never raise on it."""
    row = media.player_row(empty)
    assert row["state"] is None
    assert row["title"] is None
    assert row["volume"] is None


def test_player_row_always_returns_the_same_keys():
    """Stable columns keep the rendered table aligned across devices."""
    assert media.player_row(_playing_state()).keys() == media.player_row(None).keys()


def test_player_row_tolerates_a_non_dict_payload():
    assert media.player_row("unexpected")["state"] is None


def test_player_row_survives_partial_info():
    row = media.player_row({"playerInfo": {"state": "PAUSED", "infoText": {"title": "X"}}})
    assert row["state"] == "PAUSED"
    assert row["title"] == "X"
    assert row["artist"] is None


# ── resolve_device ──────────────────────────────────────────────────────


def test_resolve_device_prefers_the_first_online_echo_when_unnamed():
    devices = [_echo("SN-OFF", "Old Echo", online=False), _echo("SN-ON", "Kitchen", online=True)]
    fake_cls, _ = _fake_api(devices)
    with patch("alexapy.AlexaAPI", fake_cls):
        ref = _run(media.resolve_device(MagicMock(), None))
    assert isinstance(ref, DeviceRef)
    assert ref.device_serial_number == "SN-ON"


def test_resolve_device_falls_back_to_the_first_device_when_all_offline():
    devices = [_echo("SN-A", "A", online=False), _echo("SN-B", "B", online=False)]
    fake_cls, _ = _fake_api(devices)
    with patch("alexapy.AlexaAPI", fake_cls):
        ref = _run(media.resolve_device(MagicMock(), None))
    assert ref.device_serial_number == "SN-A"


def test_resolve_device_matches_by_name_case_insensitively():
    fake_cls, _ = _fake_api([_echo("SN-A", "Kitchen"), _echo("SN-B", "Bedroom")])
    with patch("alexapy.AlexaAPI", fake_cls):
        ref = _run(media.resolve_device(MagicMock(), "BEDROOM"))
    assert ref.device_serial_number == "SN-B"


def test_resolve_device_matches_by_serial():
    fake_cls, _ = _fake_api([_echo("SN-A", "Kitchen"), _echo("SN-B", "Bedroom")])
    with patch("alexapy.AlexaAPI", fake_cls):
        ref = _run(media.resolve_device(MagicMock(), "SN-B"))
    assert ref.account_name == "Bedroom"


def test_resolve_device_unknown_name_raises():
    fake_cls, _ = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="no device matching"):
            _run(media.resolve_device(MagicMock(), "Garage"))


def test_resolve_device_empty_account_raises():
    fake_cls, _ = _fake_api([])
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="no Alexa devices"):
            _run(media.resolve_device(MagicMock(), None))


# ── transport ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(media.TRANSPORT_COMMANDS))
def test_transport_calls_the_matching_alexapy_method(verb):
    api = MagicMock()
    setattr(api, media.TRANSPORT_COMMANDS[verb], AsyncMock())
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.transport(MagicMock(), "Kitchen", verb))
    getattr(api, media.TRANSPORT_COMMANDS[verb]).assert_awaited_once_with()
    assert result == {"device": "Kitchen", "action": verb, "ok": True}


def test_transport_verb_is_case_insensitive_and_trimmed():
    api = MagicMock()
    api.pause = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.transport(MagicMock(), None, "  PAUSE "))
    api.pause.assert_awaited_once()
    assert result["action"] == "pause"


def test_transport_unknown_verb_raises_before_any_network_call():
    fake_cls, _ = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="unknown media action"):
            _run(media.transport(MagicMock(), "Kitchen", "explode"))
    fake_cls.get_devices.assert_not_awaited()


def test_transport_unknown_verb_lists_the_supported_ones():
    with pytest.raises(ValueError, match="pause"):
        _run(media.transport(MagicMock(), "Kitchen", "explode"))


def test_stop_is_not_a_transport_verb():
    """stop goes through the sequence API and takes all_devices — keep it apart."""
    assert "stop" not in media.TRANSPORT_COMMANDS


# ── stop ────────────────────────────────────────────────────────────────


def test_stop_targets_one_device_by_default():
    api = MagicMock()
    api.stop = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.stop(MagicMock(), "Kitchen"))
    api.stop.assert_awaited_once_with(all_devices=False)
    assert result["device"] == "Kitchen"


def test_stop_all_devices_reports_all():
    api = MagicMock()
    api.stop = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.stop(MagicMock(), None, all_devices=True))
    api.stop.assert_awaited_once_with(all_devices=True)
    assert result["device"] == "all"


# ── volume / shuffle / repeat ───────────────────────────────────────────


def test_set_volume_sends_the_fraction_alexapy_expects():
    api = MagicMock()
    api.set_volume = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.set_volume(MagicMock(), "Kitchen", 40))
    api.set_volume.assert_awaited_once_with(pytest.approx(0.4))
    assert result == {"device": "Kitchen", "volume": 40}


def test_set_volume_rejects_a_bad_level_before_resolving_a_device():
    fake_cls, _ = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="between 0 and 100"):
            _run(media.set_volume(MagicMock(), "Kitchen", 150))
    fake_cls.get_devices.assert_not_awaited()


@pytest.mark.parametrize(("enabled", "shown"), [(True, "on"), (False, "off")])
def test_set_shuffle(enabled, shown):
    api = MagicMock()
    api.shuffle = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.set_shuffle(MagicMock(), "Kitchen", enabled))
    api.shuffle.assert_awaited_once_with(enabled)
    assert result["shuffle"] == shown


@pytest.mark.parametrize(("enabled", "shown"), [(True, "on"), (False, "off")])
def test_set_repeat(enabled, shown):
    api = MagicMock()
    api.repeat = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.set_repeat(MagicMock(), "Kitchen", enabled))
    api.repeat.assert_awaited_once_with(enabled)
    assert result["repeat"] == shown


# ── play_music ──────────────────────────────────────────────────────────


def test_play_music_passes_provider_then_phrase():
    """alexapy's signature is play_music(provider_id, search_phrase) — order matters."""
    api = MagicMock()
    api.play_music = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(media.play_music(MagicMock(), "Kitchen", "jazz radio", "spotify"))
    api.play_music.assert_awaited_once_with("SPOTIFY", "jazz radio")
    assert result == {"device": "Kitchen", "provider": "SPOTIFY", "search": "jazz radio"}


def test_play_music_defaults_the_provider():
    api = MagicMock()
    api.play_music = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        _run(media.play_music(MagicMock(), "Kitchen", "the beatles"))
    assert api.play_music.call_args[0][0] == media.DEFAULT_MUSIC_PROVIDER


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_play_music_requires_a_search_phrase(blank):
    fake_cls, _ = _fake_api([_echo()])
    with patch("alexapy.AlexaAPI", fake_cls):
        with pytest.raises(ValueError, match="search phrase is required"):
            _run(media.play_music(MagicMock(), "Kitchen", blank))
    fake_cls.get_devices.assert_not_awaited()


# ── player_status ───────────────────────────────────────────────────────


def test_player_status_returns_a_flattened_row_for_the_named_device():
    api = MagicMock()
    api.get_state = AsyncMock(return_value=_playing_state())
    fake_cls, _ = _fake_api([_echo("SN1", "Kitchen")], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(media.player_status(MagicMock(), "Kitchen"))
    assert row["device"] == "Kitchen"
    assert row["title"] == "Take Five"


def test_player_status_of_an_idle_device_is_empty_not_an_error():
    api = MagicMock()
    api.get_state = AsyncMock(return_value=None)
    fake_cls, _ = _fake_api([_echo("SN1", "Kitchen")], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(media.player_status(MagicMock(), "Kitchen"))
    assert row["device"] == "Kitchen"
    assert row["state"] is None


def test_media_operations_bind_alexaapi_to_a_device_ref():
    """Regression guard: a raw dict here raises AttributeError inside alexapy."""
    api = MagicMock()
    api.pause = AsyncMock()
    fake_cls, _ = _fake_api([_echo()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        _run(media.transport(MagicMock(), "Kitchen", "pause"))
    assert isinstance(fake_cls.call_args[0][0], DeviceRef)
