"""Behavioural tests for core/bluetooth.py and control.push.

The pure layer (MAC canonicalisation, per-Echo pairing extraction, target
resolution, the not-paired message) is tested directly.  The live wrappers are
exercised against a fake ``AlexaAPI`` so device binding, the "address is sent
verbatim" rule, the ambiguity abort and the all-or-nothing disconnect are pinned
without a live account.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import bluetooth, control
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


PHONE = {"friendlyName": "Jon's Phone", "address": "AA:BB:CC:DD:EE:FF", "connected": False}
LAPTOP = {"friendlyName": "Work Laptop", "address": "11:22:33:44:55:66", "connected": True}


def _payload(*, serial="SN1", paired=(PHONE, LAPTOP), envelope=True):
    states = [{"deviceSerialNumber": serial, "pairedDeviceList": list(paired)}]
    return {"bluetoothStates": states} if envelope else states


def _fake_api(devices, bluetooth_payload=None):
    """Fake ``AlexaAPI``: static get_devices/get_bluetooth + the two writes."""
    api = MagicMock()
    api.set_bluetooth = AsyncMock(return_value=None)
    api.disconnect_bluetooth = AsyncMock(return_value=None)
    api.send_mobilepush = AsyncMock(return_value=None)
    api.send_dropin_notification = AsyncMock(return_value=None)
    fake_cls = MagicMock()
    fake_cls.get_devices = AsyncMock(return_value=devices)
    fake_cls.get_bluetooth = AsyncMock(return_value=bluetooth_payload)
    fake_cls.return_value = api
    return fake_cls, api


# ── is_mac / normalize_mac ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF"],
)
def test_is_mac_accepts_the_three_ways_of_writing_one(value):
    assert bluetooth.is_mac(value) is True


@pytest.mark.parametrize(
    "value",
    ["Jon's Phone", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", "AA:BB-CC:DD:EE:FF", "", None, 42],
)
def test_is_mac_rejects_everything_else(value):
    assert bluetooth.is_mac(value) is False


@pytest.mark.parametrize(
    "value", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabbccddeeff", " AA:BB:CC:DD:EE:FF "]
)
def test_normalize_mac_canonicalises_every_spelling_to_the_same_string(value):
    assert bluetooth.normalize_mac(value) == "AA:BB:CC:DD:EE:FF"


def test_normalize_mac_leaves_an_opaque_sink_id_comparable():
    """Not every Alexa bluetooth address is a plain MAC — don't mangle it."""
    assert bluetooth.normalize_mac(" some-opaque-id ") == "SOME-OPAQUE-ID"


@pytest.mark.parametrize("empty", [None, "", "   ", 42, []])
def test_normalize_mac_of_nothing_is_empty(empty):
    assert bluetooth.normalize_mac(empty) == ""


@pytest.mark.parametrize(
    ("value", "expected"), [("  Jon's   Phone ", "jon's phone"), ("KITCHEN", "kitchen")]
)
def test_normalize_name_is_case_and_whitespace_insensitive(value, expected):
    assert bluetooth.normalize_name(value) == expected


@pytest.mark.parametrize("empty", [None, 42, ""])
def test_normalize_name_of_nothing_is_empty(empty):
    assert bluetooth.normalize_name(empty) == ""


# ── pairings_for ────────────────────────────────────────────────────────


def test_pairings_for_flattens_one_echos_sinks():
    rows = bluetooth.pairings_for(_payload(), "SN1")
    assert [r["name"] for r in rows] == ["Jon's Phone", "Work Laptop"]
    assert rows[0] == {
        "name": "Jon's Phone",
        "address": "AA:BB:CC:DD:EE:FF",
        "connected": False,
        "profiles": [],
    }


def test_pairings_for_accepts_the_bare_list_shape():
    rows = bluetooth.pairings_for(_payload(envelope=False), "SN1")
    assert len(rows) == 2


def test_pairings_for_ignores_other_echos():
    payload = {
        "bluetoothStates": [
            {"deviceSerialNumber": "SN1", "pairedDeviceList": [PHONE]},
            {"deviceSerialNumber": "SN2", "pairedDeviceList": [LAPTOP]},
        ]
    }
    assert [r["name"] for r in bluetooth.pairings_for(payload, "SN2")] == ["Work Laptop"]


def test_pairings_for_keeps_the_reported_profiles():
    payload = _payload(paired=[{**PHONE, "profiles": ["A2DP-SOURCE", "AVRCP"]}])
    (row,) = bluetooth.pairings_for(payload, "SN1")
    assert row["profiles"] == ["A2DP-SOURCE", "AVRCP"]


@pytest.mark.parametrize(
    "payload",
    [None, {}, "junk", 42, {"bluetoothStates": None}, {"bluetoothStates": ["junk", None]}],
)
def test_pairings_for_tolerates_junk_payloads(payload):
    assert bluetooth.pairings_for(payload, "SN1") == []


@pytest.mark.parametrize("serial", [None, "", "   "])
def test_pairings_for_without_a_serial_is_empty(serial):
    assert bluetooth.pairings_for(_payload(), serial) == []


def test_pairings_for_an_echo_with_nothing_paired_is_empty_not_an_error():
    assert bluetooth.pairings_for(_payload(paired=()), "SN1") == []
    assert bluetooth.pairings_for(_payload(paired=("junk", None)), "SN1") == []


def test_pairings_for_an_absent_echo_is_empty():
    assert bluetooth.pairings_for(_payload(), "SN9") == []


# ── resolve_pairing ─────────────────────────────────────────────────────


def _rows():
    return bluetooth.pairings_for(_payload(), "SN1")


@pytest.mark.parametrize(
    "target",
    [
        "AA:BB:CC:DD:EE:FF",  # exact address
        "aa:bb:cc:dd:ee:ff",  # normalized address
        "aa-bb-cc-dd-ee-ff",
        "AABBCCDDEEFF",
        "Jon's Phone",  # exact name
        "jon's phone",  # normalized name
        "  Jon's   Phone  ",
    ],
)
def test_resolve_pairing_finds_the_phone_by_address_or_name(target):
    (hit,) = bluetooth.resolve_pairing(_rows(), target)
    assert hit["name"] == "Jon's Phone"


@pytest.mark.parametrize("target", [None, "", "   "])
def test_resolve_pairing_of_nothing_is_no_match(target):
    assert bluetooth.resolve_pairing(_rows(), target) == []


@pytest.mark.parametrize("target", ["Garage Speaker", "99:99:99:99:99:99"])
def test_resolve_pairing_unknown_target_is_no_match(target):
    assert bluetooth.resolve_pairing(_rows(), target) == []


def test_resolve_pairing_returns_every_match_so_the_caller_can_abort():
    """Two sinks can share a friendly name; ambiguity is the caller's call."""
    rows = bluetooth.pairings_for(
        _payload(paired=[PHONE, {"friendlyName": "Jon's Phone", "address": "99:88:77:66:55:44"}]),
        "SN1",
    )
    assert len(bluetooth.resolve_pairing(rows, "jon's phone")) == 2


def test_resolve_pairing_prefers_an_address_match_over_a_name_match():
    rows = bluetooth.pairings_for(
        _payload(
            paired=[{"friendlyName": "AA:BB:CC:DD:EE:FF", "address": "11:11:11:11:11:11"}, PHONE]
        ),
        "SN1",
    )
    (hit,) = bluetooth.resolve_pairing(rows, "AA:BB:CC:DD:EE:FF")
    assert hit["address"] == "AA:BB:CC:DD:EE:FF"


def test_resolve_pairing_skips_non_dict_rows():
    assert bluetooth.resolve_pairing(["junk", None, *_rows()], "Work Laptop")


@pytest.mark.parametrize("empty", [None, []])
def test_resolve_pairing_with_no_pairings_is_no_match(empty):
    assert bluetooth.resolve_pairing(empty, "Jon's Phone") == []


# ── pairing_names / not_paired_error ────────────────────────────────────


def test_pairing_names_labels_by_name_then_address():
    rows = bluetooth.pairings_for(_payload(paired=[PHONE, {"address": "00:00:00:00:00:01"}]), "SN1")
    assert bluetooth.pairing_names(rows) == ["Jon's Phone", "00:00:00:00:00:01"]


def test_pairing_names_skips_junk_and_unlabelled_rows():
    assert bluetooth.pairing_names(["junk", None, {}, {"name": "X"}]) == ["X"]


def test_not_paired_error_names_the_alternatives():
    message = bluetooth.not_paired_error("Kitchen", "Garage Speaker", _rows())
    assert "Garage Speaker" in message
    assert "Jon's Phone" in message
    assert "Kitchen" in message


def test_not_paired_error_with_nothing_paired_points_at_the_app():
    """Initial pairing is app/voice-only, so say so instead of listing nothing."""
    message = bluetooth.not_paired_error("Kitchen", "Jon's Phone", [])
    assert "nothing is paired" in message
    assert "Alexa app" in message


def test_not_paired_error_without_a_device_name_still_reads():
    assert "no paired device matching" in bluetooth.not_paired_error(None, "X", _rows())


# ── live: list_pairings ─────────────────────────────────────────────────


def test_list_pairings_defaults_to_the_first_online_echo():
    fake_cls, _api = _fake_api(
        [_echo("SN0", "Study", online=False), _echo("SN1", "Kitchen")], _payload()
    )
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(bluetooth.list_pairings(MagicMock()))
    assert result["device"] == "Kitchen"
    assert result["serial"] == "SN1"
    assert [p["name"] for p in result["pairings"]] == ["Jon's Phone", "Work Laptop"]


def test_list_pairings_of_a_named_echo_with_nothing_paired():
    fake_cls, _api = _fake_api([_echo("SN1", "Kitchen")], _payload(serial="SN2"))
    with patch("alexapy.AlexaAPI", fake_cls):
        result = _run(bluetooth.list_pairings(MagicMock(), "Kitchen"))
    assert result["pairings"] == []


# ── live: connect ───────────────────────────────────────────────────────


def test_connect_sends_amazons_own_address_verbatim():
    """The pairing list's `address` is what pair-sink wants — never our own form."""
    fake_cls, api = _fake_api([_echo()], _payload())
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(bluetooth.connect(MagicMock(), "Kitchen", "jon's phone"))
    api.set_bluetooth.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")
    assert row == {
        "device": "Kitchen",
        "connected": "Jon's Phone",
        "address": "AA:BB:CC:DD:EE:FF",
        "ok": True,
    }
    assert isinstance(fake_cls.call_args.args[0], DeviceRef)  # never the raw dict


def test_connect_accepts_a_differently_spelled_mac():
    fake_cls, api = _fake_api([_echo()], _payload())
    with patch("alexapy.AlexaAPI", fake_cls):
        _run(bluetooth.connect(MagicMock(), None, "aa-bb-cc-dd-ee-ff"))
    api.set_bluetooth.assert_awaited_once_with("AA:BB:CC:DD:EE:FF")


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_connect_without_a_target_is_refused_before_the_network(blank):
    fake_cls, api = _fake_api([_echo()], _payload())
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="is required"):
        _run(bluetooth.connect(MagicMock(), "Kitchen", blank))
    fake_cls.get_devices.assert_not_awaited()
    api.set_bluetooth.assert_not_awaited()


def test_connect_to_an_unpaired_target_lists_what_is_paired():
    fake_cls, api = _fake_api([_echo()], _payload())
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError) as err:
        _run(bluetooth.connect(MagicMock(), "Kitchen", "Garage Speaker"))
    assert "Jon's Phone" in str(err.value)
    api.set_bluetooth.assert_not_awaited()


def test_connect_with_nothing_paired_points_at_the_alexa_app():
    fake_cls, api = _fake_api([_echo()], _payload(paired=()))
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="Alexa app"):
        _run(bluetooth.connect(MagicMock(), "Kitchen", "Jon's Phone"))
    api.set_bluetooth.assert_not_awaited()


def test_connect_aborts_on_an_ambiguous_name_and_lists_the_addresses():
    twin = {"friendlyName": "Jon's Phone", "address": "99:88:77:66:55:44"}
    fake_cls, api = _fake_api([_echo()], _payload(paired=[PHONE, twin]))
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError) as err:
        _run(bluetooth.connect(MagicMock(), "Kitchen", "Jon's Phone"))
    message = str(err.value)
    assert "matches 2 paired devices" in message
    assert "99:88:77:66:55:44" in message
    api.set_bluetooth.assert_not_awaited()


def test_connect_refuses_a_pairing_with_no_address():
    fake_cls, api = _fake_api([_echo()], _payload(paired=[{"friendlyName": "Mystery Sink"}]))
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="has no address"):
        _run(bluetooth.connect(MagicMock(), "Kitchen", "Mystery Sink"))
    api.set_bluetooth.assert_not_awaited()


def test_connect_to_an_unknown_echo_is_a_value_error():
    fake_cls, api = _fake_api([_echo("SN1", "Kitchen")], _payload())
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="no device matching"):
        _run(bluetooth.connect(MagicMock(), "Garage", "Jon's Phone"))
    api.set_bluetooth.assert_not_awaited()


# ── live: disconnect ────────────────────────────────────────────────────


def test_disconnect_drops_every_sink_and_says_so():
    """Amazon has no per-sink disconnect — the row must not imply otherwise."""
    fake_cls, api = _fake_api([_echo()], _payload())
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(bluetooth.disconnect(MagicMock(), "Kitchen"))
    api.disconnect_bluetooth.assert_awaited_once_with()
    assert row == {"device": "Kitchen", "disconnected": "all", "ok": True}


def test_disconnect_defaults_to_the_first_online_echo():
    fake_cls, api = _fake_api([_echo("SN0", "Study", online=False), _echo("SN1", "Kitchen")], None)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(bluetooth.disconnect(MagicMock()))
    assert row["device"] == "Kitchen"
    api.disconnect_bluetooth.assert_awaited_once()


def test_disconnect_with_no_devices_on_the_account_is_a_value_error():
    fake_cls, api = _fake_api([], None)
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="no Alexa devices"):
        _run(bluetooth.disconnect(MagicMock()))
    api.disconnect_bluetooth.assert_not_awaited()


# ── control.normalize_push ──────────────────────────────────────────────


def test_normalize_push_trims_and_defaults_the_title():
    assert control.normalize_push("  the washing is done  ") == (
        "the washing is done",
        control.DEFAULT_PUSH_TITLE,
    )


@pytest.mark.parametrize("blank_title", [None, "", "   "])
def test_normalize_push_blank_title_falls_back_to_our_own_not_alexapys(blank_title):
    _text, title = control.normalize_push("hello", blank_title)
    assert title == control.DEFAULT_PUSH_TITLE
    assert title != "AlexaAPI Message"  # alexapy's developer-facing default


def test_normalize_push_keeps_an_explicit_title():
    assert control.normalize_push("hello", " Laundry ") == ("hello", "Laundry")


@pytest.mark.parametrize("blank", [None, "", "   ", "\n"])
def test_normalize_push_rejects_an_empty_message(blank):
    with pytest.raises(ValueError, match="a message is required"):
        control.normalize_push(blank)


# ── control.push ────────────────────────────────────────────────────────


def test_push_sends_a_mobile_push_via_the_first_online_echo():
    fake_cls, api = _fake_api([_echo("SN0", "Study", online=False), _echo("SN1", "Kitchen")], None)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(control.push(MagicMock(), "  the oven is done  "))
    api.send_mobilepush.assert_awaited_once_with(
        "the oven is done", title=control.DEFAULT_PUSH_TITLE
    )
    api.send_dropin_notification.assert_not_awaited()
    assert row == {
        "pushed": "the oven is done",
        "title": control.DEFAULT_PUSH_TITLE,
        "kind": "mobilepush",
        "via_device": "Kitchen",
    }
    assert isinstance(fake_cls.call_args.args[0], DeviceRef)


def test_push_dropin_uses_the_dropin_call():
    fake_cls, api = _fake_api([_echo()], None)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(control.push(MagicMock(), "look at this", title="Cam", dropin=True))
    api.send_dropin_notification.assert_awaited_once_with("look at this", title="Cam")
    api.send_mobilepush.assert_not_awaited()
    assert row["kind"] == "dropin"


def test_push_to_a_named_device():
    fake_cls, api = _fake_api([_echo("SN1", "Kitchen"), _echo("SN2", "Study")], None)
    with patch("alexapy.AlexaAPI", fake_cls):
        row = _run(control.push(MagicMock(), "hi", device="Study"))
    assert row["via_device"] == "Study"
    api.send_mobilepush.assert_awaited_once()


def test_push_validates_the_message_before_touching_the_network():
    fake_cls, api = _fake_api([_echo()], None)
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="a message is"):
        _run(control.push(MagicMock(), "   "))
    fake_cls.get_devices.assert_not_awaited()
    api.send_mobilepush.assert_not_awaited()


def test_push_with_no_devices_on_the_account_is_a_value_error():
    fake_cls, api = _fake_api([], None)
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="no Alexa devices"):
        _run(control.push(MagicMock(), "hello"))
    api.send_mobilepush.assert_not_awaited()


def test_push_to_an_unknown_device_is_a_value_error():
    fake_cls, api = _fake_api([_echo("SN1", "Kitchen")], None)
    with patch("alexapy.AlexaAPI", fake_cls), pytest.raises(ValueError, match="no device matching"):
        _run(control.push(MagicMock(), "hello", device="Garage"))
    api.send_mobilepush.assert_not_awaited()
