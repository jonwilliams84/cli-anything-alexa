"""Behavioural tests for the DeviceRef adapter.

``DeviceRef`` exists because alexapy's device-bound ``AlexaAPI`` methods read
their target as **attributes** off ``self._device`` while ``get_devices()``
hands back **dicts**.  These tests pin that translation, and — most importantly
— assert the *contract*: the exact attribute names alexapy dereferences must
exist, because a rename or a typo there is invisible until a live call blows up
with ``AttributeError``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import control, routines
from cli_anything.alexa.core.device_ref import DEFAULT_LOCALE, DeviceRef, to_device_ref


def _record(**overrides):
    rec = {
        "serialNumber": "G090LF1234567890",
        "accountName": "Kitchen Echo",
        "deviceType": "A3S5BH2HU6VAYF",
        "deviceFamily": "ECHO",
        "online": True,
    }
    rec.update(overrides)
    return rec


# ── the alexapy contract ────────────────────────────────────────────────

#: Every attribute alexapy's AlexaAPI instance methods dereference on
#: ``self._device`` (grepped from alexaapi.py: set_media, set_dnd_state, stop,
#: process_targets, send_announcement/send_tts, run_routine, bluetooth).
ALEXAPY_REQUIRED_ATTRS = (
    "device_serial_number",
    "_device_type",
    "_device_family",
    "_cluster_members",
    "_locale",
)


@pytest.mark.parametrize("attr", ALEXAPY_REQUIRED_ATTRS)
def test_device_ref_exposes_every_attribute_alexapy_dereferences(attr):
    """The adapter must satisfy alexapy's private AlexaClient contract."""
    ref = to_device_ref(_record())
    assert hasattr(ref, attr), f"alexapy reads self._device.{attr}; DeviceRef must provide it"


def test_raw_dict_does_not_satisfy_the_contract():
    """Guard the reason this adapter exists: a plain record has none of it.

    If this ever passes, alexapy changed to dict access and the adapter could
    be reconsidered.
    """
    record = _record()
    for attr in ALEXAPY_REQUIRED_ATTRS:
        assert not hasattr(record, attr)


# ── field translation ───────────────────────────────────────────────────


def test_translates_every_field_from_the_record():
    ref = to_device_ref(_record())
    assert ref.device_serial_number == "G090LF1234567890"
    assert ref._device_type == "A3S5BH2HU6VAYF"
    assert ref._device_family == "ECHO"
    assert ref.account_name == "Kitchen Echo"
    assert ref.online is True


def test_missing_optional_fields_become_empty_not_none():
    """alexapy string-concatenates _device_type (bluetooth paths), so '' beats None."""
    ref = to_device_ref({"serialNumber": "SN1"})
    assert ref._device_type == ""
    assert ref._device_family == ""
    assert ref._cluster_members == []
    assert ref.online is False


def test_serial_is_stripped_of_whitespace():
    assert to_device_ref(_record(serialNumber="  SN1  ")).device_serial_number == "SN1"


def test_missing_serial_raises_a_caller_facing_error():
    """A device with no serial cannot be addressed — fail loudly, not silently."""
    with pytest.raises(ValueError, match="no serialNumber"):
        to_device_ref({"accountName": "Ghost Echo"})


def test_missing_serial_error_names_the_device():
    with pytest.raises(ValueError, match="Ghost Echo"):
        to_device_ref({"accountName": "Ghost Echo"})


def test_blank_serial_raises():
    with pytest.raises(ValueError, match="no serialNumber"):
        to_device_ref({"serialNumber": "   ", "accountName": "Blank"})


def test_none_record_raises():
    with pytest.raises(ValueError, match="no serialNumber"):
        to_device_ref(None)


# ── locale fallback ─────────────────────────────────────────────────────


def test_locale_property_falls_back_to_en_us():
    """Mirrors alexapy's own `self._device._locale if ... else 'en-US'`."""
    assert to_device_ref(_record()).locale == DEFAULT_LOCALE


def test_locale_property_uses_the_record_locale_when_present():
    assert to_device_ref(_record(locale="en-GB")).locale == "en-GB"


def test_private_locale_stays_falsy_so_alexapy_applies_its_own_default():
    assert to_device_ref(_record())._locale is None


# ── whole-home audio ────────────────────────────────────────────────────


def test_wha_group_is_flagged_and_carries_cluster_members():
    ref = to_device_ref(
        _record(deviceFamily="WHA", clusterMembers=["SN-A", "SN-B"], accountName="Downstairs")
    )
    assert ref.is_wha is True
    assert ref._cluster_members == ["SN-A", "SN-B"]


def test_normal_echo_is_not_wha():
    assert to_device_ref(_record()).is_wha is False


# ── isolation + summary ─────────────────────────────────────────────────


def test_raw_record_is_copied_not_aliased():
    """Mutating the adapter's copy must not corrupt the caller's device list."""
    record = _record()
    ref = to_device_ref(record)
    ref.raw["accountName"] = "Changed"
    assert record["accountName"] == "Kitchen Echo"


def test_summary_is_json_safe_identity():
    summary = to_device_ref(_record()).summary()
    assert summary == {
        "device": "Kitchen Echo",
        "serial": "G090LF1234567890",
        "deviceType": "A3S5BH2HU6VAYF",
        "deviceFamily": "ECHO",
        "online": True,
    }


def test_direct_construction_matches_the_helper():
    assert (
        DeviceRef(_record()).device_serial_number == to_device_ref(_record()).device_serial_number
    )


# ── the call sites actually pass an adapter ─────────────────────────────


def _fake_api_class(devices, api_instance):
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.get_automations = AsyncMock(return_value=[])
    fake_cls.return_value = api_instance
    return fake_cls


def _constructed_device(fake_cls):
    """The first positional arg AlexaAPI(...) was built with."""
    return fake_cls.call_args[0][0]


def test_announce_binds_alexaapi_to_a_device_ref_not_a_dict():
    api = MagicMock()
    api.send_announcement = AsyncMock()
    fake_cls = _fake_api_class([_record()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        asyncio.run(control.announce(MagicMock(), "hello"))
    assert isinstance(_constructed_device(fake_cls), DeviceRef)


def test_set_dnd_binds_alexaapi_to_a_device_ref_not_a_dict():
    api = MagicMock()
    api.set_dnd_state = AsyncMock()
    fake_cls = _fake_api_class([_record()], api)
    with patch("alexapy.AlexaAPI", fake_cls):
        asyncio.run(control.set_dnd(MagicMock(), "Kitchen Echo", True))
    assert isinstance(_constructed_device(fake_cls), DeviceRef)


def test_run_routine_binds_alexaapi_to_a_device_ref_not_a_dict():
    api = MagicMock()
    api.run_routine = AsyncMock()
    automations = [
        {
            "automationId": "a1",
            "name": "Good Morning",
            "triggers": [{"payload": {"utterance": "good morning"}}],
        }
    ]
    fake_cls = _fake_api_class([_record()], api)
    fake_cls.get_automations = AsyncMock(return_value=automations)
    with patch("alexapy.AlexaAPI", fake_cls):
        asyncio.run(routines.run_routine(MagicMock(), "good morning"))
    assert isinstance(_constructed_device(fake_cls), DeviceRef)


def test_bound_device_ref_carries_the_targeted_serial():
    """The adapter must describe the device the user asked for, not another."""
    api = MagicMock()
    api.set_dnd_state = AsyncMock()
    devices = [_record(serialNumber="SN-A", accountName="Kitchen")]
    devices.append(_record(serialNumber="SN-B", accountName="Bedroom"))
    fake_cls = _fake_api_class(devices, api)
    with patch("alexapy.AlexaAPI", fake_cls):
        asyncio.run(control.set_dnd(MagicMock(), "bedroom", False))
    assert _constructed_device(fake_cls).device_serial_number == "SN-B"
