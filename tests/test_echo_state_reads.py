"""Behavioural tests for the Echo state reads: bluetooth, wake words, DND.

These three sit on ``AlexaAPI`` *static* endpoints and each returns a slightly
different envelope — alexapy already unwraps ``wakeWords`` but hands back the
full document for bluetooth and DND.  The row builders normalise all of that,
and join serialNumber -> accountName so output is readable rather than a wall
of serials.  ``control.speak`` (send_tts) is covered here too since it is the
other new device-bound read/write on the same surface.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import control, devices_meta
from cli_anything.alexa.core.device_ref import DeviceRef


def _run(coro):
    return asyncio.run(coro)


DEVICES = [
    {"serialNumber": "SN1", "accountName": "Kitchen", "online": True},
    {"serialNumber": "SN2", "accountName": "Bedroom", "online": True},
]


# ── bluetooth_rows ──────────────────────────────────────────────────────


def _bt_payload():
    return {
        "bluetoothStates": [
            {
                "deviceSerialNumber": "SN1",
                "pairedDeviceList": [
                    {"friendlyName": "Pixel", "address": "AA:BB:CC", "connected": True},
                    {"friendlyName": "Laptop", "address": "DD:EE:FF", "connected": False},
                ],
            },
            {"deviceSerialNumber": "SN2", "pairedDeviceList": []},
        ]
    }


def test_bluetooth_rows_emits_one_row_per_paired_device():
    rows = devices_meta.bluetooth_rows(_bt_payload(), DEVICES)
    paired = [r for r in rows if r["paired"]]
    assert [r["paired"] for r in paired] == ["Pixel", "Laptop"]
    assert paired[0]["connected"] is True
    assert paired[1]["connected"] is False


def test_bluetooth_rows_resolves_serial_to_account_name():
    rows = devices_meta.bluetooth_rows(_bt_payload(), DEVICES)
    assert rows[0]["device"] == "Kitchen"


def test_bluetooth_rows_keeps_an_echo_with_nothing_paired():
    """Omitting it would look like the device was missed, not that it is empty."""
    rows = devices_meta.bluetooth_rows(_bt_payload(), DEVICES)
    bedroom = [r for r in rows if r["serial"] == "SN2"]
    assert len(bedroom) == 1
    assert bedroom[0]["paired"] is None


def test_bluetooth_rows_falls_back_to_the_serial_when_the_name_is_unknown():
    rows = devices_meta.bluetooth_rows(_bt_payload(), [])
    assert rows[0]["device"] == "SN1"


@pytest.mark.parametrize("empty", [None, {}, {"bluetoothStates": []}, []])
def test_bluetooth_rows_of_an_empty_payload_is_empty(empty):
    assert devices_meta.bluetooth_rows(empty, DEVICES) == []


def test_bluetooth_rows_accepts_a_bare_list_envelope():
    """Defensive: alexapy unwraps some payloads and not others."""
    rows = devices_meta.bluetooth_rows(
        [{"deviceSerialNumber": "SN1", "pairedDeviceList": [{"friendlyName": "Pixel"}]}], DEVICES
    )
    assert rows[0]["paired"] == "Pixel"


def test_bluetooth_rows_skips_non_dict_entries():
    rows = devices_meta.bluetooth_rows({"bluetoothStates": ["junk", None]}, DEVICES)
    assert rows == []


# ── wake_word_rows ──────────────────────────────────────────────────────


def test_wake_word_rows_from_the_unwrapped_list_alexapy_returns():
    rows = devices_meta.wake_word_rows(
        [{"deviceSerialNumber": "SN1", "wakeWord": "ALEXA", "active": True}], DEVICES
    )
    assert rows == [{"device": "Kitchen", "serial": "SN1", "wakeWord": "ALEXA", "active": True}]


def test_wake_word_rows_from_the_wrapped_envelope():
    rows = devices_meta.wake_word_rows(
        {"wakeWords": [{"deviceSerialNumber": "SN2", "wakeWord": "ECHO"}]}, DEVICES
    )
    assert rows[0]["device"] == "Bedroom"
    assert rows[0]["wakeWord"] == "ECHO"


@pytest.mark.parametrize("empty", [None, {}, []])
def test_wake_word_rows_of_an_empty_payload_is_empty(empty):
    assert devices_meta.wake_word_rows(empty, DEVICES) == []


# ── dnd_rows ────────────────────────────────────────────────────────────


def test_dnd_rows_render_on_off_matching_the_write_command_vocabulary():
    rows = devices_meta.dnd_rows(
        {
            "doNotDisturbDeviceStatusList": [
                {"deviceSerialNumber": "SN1", "enabled": True},
                {"deviceSerialNumber": "SN2", "enabled": False},
            ]
        },
        DEVICES,
    )
    assert [r["dnd"] for r in rows] == ["on", "off"]
    assert [r["device"] for r in rows] == ["Kitchen", "Bedroom"]


def test_dnd_rows_unknown_state_stays_none_rather_than_guessing_off():
    rows = devices_meta.dnd_rows({"doNotDisturbDeviceStatusList": [{"deviceSerialNumber": "SN1"}]})
    assert rows[0]["dnd"] is None


@pytest.mark.parametrize("empty", [None, {}, [], {"doNotDisturbDeviceStatusList": []}])
def test_dnd_rows_of_an_empty_payload_is_empty(empty):
    assert devices_meta.dnd_rows(empty, DEVICES) == []


# ── the async fetchers ──────────────────────────────────────────────────


def _fake_cls(**payloads):
    fake = MagicMock()
    fake.get_devices = AsyncMock(return_value=DEVICES)
    for name, value in payloads.items():
        setattr(fake, name, AsyncMock(return_value=value))
    return fake


def test_fetch_bluetooth_joins_names_onto_the_live_payload():
    fake = _fake_cls(get_bluetooth=_bt_payload())
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(devices_meta.fetch_bluetooth(MagicMock()))
    fake.get_bluetooth.assert_awaited_once()
    assert rows[0]["device"] == "Kitchen"


def test_fetch_wake_words_joins_names_onto_the_live_payload():
    fake = _fake_cls(get_wake_words=[{"deviceSerialNumber": "SN2", "wakeWord": "ALEXA"}])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(devices_meta.fetch_wake_words(MagicMock()))
    assert rows[0]["device"] == "Bedroom"


def test_fetch_dnd_states_joins_names_onto_the_live_payload():
    fake = _fake_cls(
        get_dnd_state={
            "doNotDisturbDeviceStatusList": [{"deviceSerialNumber": "SN1", "enabled": True}]
        }
    )
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(devices_meta.fetch_dnd_states(MagicMock()))
    assert rows == [{"device": "Kitchen", "serial": "SN1", "dnd": "on"}]


def test_fetchers_survive_an_endpoint_returning_nothing():
    fake = _fake_cls(get_bluetooth=None)
    with patch("alexapy.AlexaAPI", fake):
        assert _run(devices_meta.fetch_bluetooth(MagicMock())) == []


# ── control.speak (send_tts) ────────────────────────────────────────────


def _speak_cls(devices, api):
    fake = MagicMock()
    fake.get_devices = AsyncMock(return_value=devices)
    fake.return_value = api
    return fake


def test_speak_uses_send_tts_not_send_announcement():
    """The whole point of `speak` is skipping the announcement chime."""
    api = MagicMock()
    api.send_tts = AsyncMock()
    api.send_announcement = AsyncMock()
    with patch("alexapy.AlexaAPI", _speak_cls(DEVICES, api)):
        result = _run(control.speak(MagicMock(), "dinner is ready", "Bedroom"))
    api.send_tts.assert_awaited_once_with("dinner is ready")
    api.send_announcement.assert_not_awaited()
    assert result == {"spoke": "dinner is ready", "device": "Bedroom", "serial": "SN2"}


def test_speak_defaults_to_the_first_online_device():
    api = MagicMock()
    api.send_tts = AsyncMock()
    devices = [
        {"serialNumber": "SN-OFF", "accountName": "Old", "online": False},
        {"serialNumber": "SN-ON", "accountName": "Kitchen", "online": True},
    ]
    with patch("alexapy.AlexaAPI", _speak_cls(devices, api)):
        result = _run(control.speak(MagicMock(), "hello"))
    assert result["device"] == "Kitchen"


def test_speak_falls_back_to_the_first_device_when_all_offline():
    api = MagicMock()
    api.send_tts = AsyncMock()
    devices = [{"serialNumber": "SN-A", "accountName": "Only", "online": False}]
    with patch("alexapy.AlexaAPI", _speak_cls(devices, api)):
        assert _run(control.speak(MagicMock(), "hi"))["device"] == "Only"


def test_speak_binds_alexaapi_to_a_device_ref():
    api = MagicMock()
    api.send_tts = AsyncMock()
    fake = _speak_cls(DEVICES, api)
    with patch("alexapy.AlexaAPI", fake):
        _run(control.speak(MagicMock(), "hi"))
    assert isinstance(fake.call_args[0][0], DeviceRef)


def test_speak_unknown_device_raises():
    api = MagicMock()
    api.send_tts = AsyncMock()
    with patch("alexapy.AlexaAPI", _speak_cls(DEVICES, api)):
        with pytest.raises(ValueError, match="no device matching"):
            _run(control.speak(MagicMock(), "hi", "Garage"))


def test_speak_with_no_devices_raises():
    api = MagicMock()
    with patch("alexapy.AlexaAPI", _speak_cls([], api)):
        with pytest.raises(ValueError, match="no Alexa devices"):
            _run(control.speak(MagicMock(), "hi"))
