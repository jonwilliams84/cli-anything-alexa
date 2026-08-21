"""Behavioural tests for the newly wrapped introspection reads.

Three previously unwrapped alexapy calls:

* ``get_device_preferences`` → ``echos preferences`` (and the timezone an
  alarm edit rewrites its local wall-clock fields in);
* ``get_wifi_details`` → ``echos wifi`` (device-bound, so it must go through
  ``DeviceRef``);
* ``get_authentication`` → ``auth whoami``.

The row builders are pure and tested directly; the wrappers run against a fake
``AlexaAPI``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import devices_meta, session
from cli_anything.alexa.core.device_ref import DeviceRef


def _run(coro):
    return asyncio.run(coro)


ECHO = {
    "serialNumber": "SN1",
    "accountName": "Kitchen",
    "deviceType": "DT1",
    "deviceFamily": "ECHO",
    "online": True,
}

PREFS = {
    "devicePreferences": [
        {
            "deviceSerialNumber": "SN1",
            "timeZoneId": "Europe/London",
            "locale": "en-GB",
            "temperatureUnit": "CELSIUS",
            "distanceUnits": "METRIC",
            "goldfishEnabled": True,
            "postalCode": "SW1A 1AA",
        }
    ]
}


# ── preference_rows ──────────────────────────────────────────────────────


def test_preference_rows_names_the_device_from_the_device_list():
    rows = devices_meta.preference_rows(PREFS, [ECHO])
    assert rows[0]["device"] == "Kitchen"
    assert rows[0]["timeZoneId"] == "Europe/London"
    assert rows[0]["temperatureUnit"] == "CELSIUS"


def test_preference_rows_falls_back_to_the_serial_when_the_device_is_unknown():
    assert devices_meta.preference_rows(PREFS, [])[0]["device"] == "SN1"


def test_preference_rows_accepts_a_bare_list_as_well_as_the_envelope():
    rows = devices_meta.preference_rows(PREFS["devicePreferences"], [ECHO])
    assert rows[0]["serial"] == "SN1"


def test_preference_rows_of_an_empty_or_missing_payload_is_empty():
    assert devices_meta.preference_rows(None) == []
    assert devices_meta.preference_rows({}) == []


def test_preference_rows_reads_the_other_wake_word_confirmation_spelling():
    payload = {"devicePreferences": [{"deviceSerialNumber": "SN1", "wakeWordConfirmation": False}]}
    assert devices_meta.preference_rows(payload)[0]["wakeWordConfirmation"] is False


# ── device_timezone ──────────────────────────────────────────────────────


def test_device_timezone_finds_the_serials_zone():
    rows = devices_meta.preference_rows(PREFS, [ECHO])
    assert devices_meta.device_timezone(rows, "SN1") == "Europe/London"


@pytest.mark.parametrize("serial", [None, "", "SN-OTHER"])
def test_device_timezone_is_none_when_it_cannot_be_answered(serial):
    rows = devices_meta.preference_rows(PREFS, [ECHO])
    assert devices_meta.device_timezone(rows, serial) is None


def test_device_timezone_of_a_device_with_a_blank_zone_is_none():
    assert devices_meta.device_timezone([{"serial": "SN1", "timeZoneId": ""}], "SN1") is None


# ── wifi_row ─────────────────────────────────────────────────────────────


def test_wifi_row_flattens_the_fields_the_cli_shows():
    row = devices_meta.wifi_row(
        {"ssid": "home-wifi", "signalStrength": 4, "securityMethod": "WPA_PSK"},
        device="Kitchen",
        serial="SN1",
    )
    assert row["ssid"] == "home-wifi"
    assert row["device"] == "Kitchen"
    assert row["serial"] == "SN1"


def test_wifi_row_unwraps_a_single_key_envelope():
    row = devices_meta.wifi_row({"deviceWifiDetails": {"essid": "home-wifi", "rssi": -55}})
    assert row["ssid"] == "home-wifi"
    assert row["signalStrength"] == -55


def test_wifi_row_of_an_empty_payload_is_all_none_rather_than_raising():
    row = devices_meta.wifi_row(None, device="Kitchen")
    assert row["device"] == "Kitchen"
    assert row["ssid"] is None
    assert row["macAddress"] is None


# ── the live wrappers ────────────────────────────────────────────────────


def test_fetch_device_preferences_names_devices_it_fetched_itself():
    fake = MagicMock()
    fake.get_device_preferences = AsyncMock(return_value=PREFS)
    fake.get_devices = AsyncMock(return_value=[ECHO])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(devices_meta.fetch_device_preferences(MagicMock()))
    assert rows[0]["device"] == "Kitchen"
    fake.get_devices.assert_awaited_once()


def test_fetch_device_preferences_reuses_a_device_list_it_was_given():
    fake = MagicMock()
    fake.get_device_preferences = AsyncMock(return_value=PREFS)
    fake.get_devices = AsyncMock(return_value=[ECHO])
    with patch("alexapy.AlexaAPI", fake):
        rows = _run(devices_meta.fetch_device_preferences(MagicMock(), [ECHO]))
    assert rows[0]["device"] == "Kitchen"
    fake.get_devices.assert_not_awaited()


def test_fetch_wifi_details_binds_the_api_to_a_device_ref_not_the_raw_dict():
    api = MagicMock()
    api.get_wifi_details = AsyncMock(return_value={"ssid": "home-wifi"})
    fake = MagicMock(return_value=api)
    fake.get_devices = AsyncMock(return_value=[ECHO])
    with patch("alexapy.AlexaAPI", fake):
        row = _run(devices_meta.fetch_wifi_details(MagicMock()))
    assert row == {
        "device": "Kitchen",
        "serial": "SN1",
        "ssid": "home-wifi",
        "signalStrength": None,
        "securityMethod": None,
        "macAddress": None,
        "ipAddress": None,
        "frequency": None,
    }
    bound = fake.call_args.args[0]
    assert isinstance(bound, DeviceRef)


def test_fetch_wifi_details_refuses_an_unknown_device():
    fake = MagicMock()
    fake.get_devices = AsyncMock(return_value=[ECHO])
    with patch("alexapy.AlexaAPI", fake), pytest.raises(ValueError, match="no device matching"):
        _run(devices_meta.fetch_wifi_details(MagicMock(), "Bathroom"))


# ── account identity ─────────────────────────────────────────────────────


def test_account_row_flattens_the_identity_alexapy_returns():
    row = session.account_row(
        {
            "authenticated": True,
            "customerEmail": "you@example.com",
            "customerId": "A1CUSTOMER",
            "customerName": "Jon",
            "canAccessPrimeMusicContent": False,
        }
    )
    assert row == {
        "authenticated": True,
        "email": "you@example.com",
        "customerId": "A1CUSTOMER",
        "name": "Jon",
        "primeMusic": False,
    }


@pytest.mark.parametrize("payload", [None, {}, "nope"])
def test_account_row_of_an_empty_answer_is_not_authenticated(payload):
    assert session.account_row(payload)["authenticated"] is False


def test_account_info_reads_users_me():
    fake = MagicMock()
    fake.get_authentication = AsyncMock(return_value={"authenticated": True, "customerId": "A1"})
    with patch("alexapy.AlexaAPI", fake):
        row = _run(session.account_info(MagicMock()))
    assert row["authenticated"] is True
    assert row["customerId"] == "A1"
