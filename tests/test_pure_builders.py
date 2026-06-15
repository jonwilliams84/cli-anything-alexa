"""Unit tests for the remaining pure helpers (no alexapy / no network):
notification payload builders + row flatteners, routine/device matching,
group rows, and session path/csrf helpers.
"""

import json
import time
from pathlib import Path

from cli_anything.alexa.core import notifications, routines, devices_meta, groups, session


# ── notifications ──────────────────────────────────────────────────────

def test_build_reminder():
    p = notifications.build_reminder("milk", "SERIAL1", "ECHO", 1700000000000)
    assert p["type"] == "Reminder"
    assert p["reminderLabel"] == "milk"
    assert p["deviceSerialNumber"] == "SERIAL1"
    assert p["alarmTime"] == 1700000000000


def test_build_timer():
    p = notifications.build_timer("S", "T", 60000, label="pasta")
    assert p["type"] == "Timer"
    assert p["remainingTime"] == 60000
    assert p["timerLabel"] == "pasta"


def test_build_alarm_label_optional():
    p = notifications.build_alarm("S", "T", 123, label="")
    assert p["type"] == "Alarm" and p["originalLabel"] is None


def test_epoch_ms_absolute_wins():
    assert notifications._epoch_ms(seconds_from_now=10, at_epoch_ms=999) == 999


def test_epoch_ms_relative():
    before = int(time.time() * 1000)
    got = notifications._epoch_ms(seconds_from_now=5)
    assert got >= before + 4000


def test_notification_rows():
    raw = [{"notificationIndex": "n1", "type": "Timer", "status": "ON",
            "timerLabel": "eggs", "deviceSerialNumber": "S"}]
    rows = notifications.notification_rows(raw)
    assert rows[0]["id"] == "n1" and rows[0]["label"] == "eggs"


# ── routines ───────────────────────────────────────────────────────────

def _automation(aid, name, utterance=None):
    triggers = []
    if utterance:
        triggers = [{"payload": {"utterance": utterance}}]
    return {"automationId": aid, "name": name, "status": "ENABLED", "triggers": triggers}


def test_routine_rows_extracts_utterance():
    rows = routines.routine_rows([_automation("a1", "Good Night", "good night")])
    assert rows[0]["utterance"] == "good night"


def test_find_routine_by_id_name_utterance():
    autos = [_automation("a1", "Good Night", "alexa good night"),
             _automation("a2", "Movie Time", "movie time")]
    assert routines.find_routine(autos, "a2")["name"] == "Movie Time"
    assert routines.find_routine(autos, "good night")["automationId"] == "a1"
    assert routines.find_routine(autos, "movie time")["automationId"] == "a2"
    assert routines.find_routine(autos, "nope") is None


# ── devices_meta ───────────────────────────────────────────────────────

def test_device_rows_and_find():
    devs = [
        {"accountName": "Kitchen Echo", "serialNumber": "AAA", "deviceType": "X",
         "deviceFamily": "ECHO", "online": True},
        {"accountName": "Office", "serialNumber": "BBB", "online": False},
    ]
    rows = devices_meta.device_rows(devs)
    assert rows[0]["accountName"] == "Kitchen Echo"
    assert devices_meta.find_device(devs, "office")["serialNumber"] == "BBB"
    assert devices_meta.find_device(devs, "AAA")["accountName"] == "Kitchen Echo"
    assert devices_meta.find_device(devs, "missing") is None


# ── groups (GraphQL device-groups) ─────────────────────────────────────

def _gql_group(gid, name, members):
    """Build a listDeviceGroups-shaped raw group record."""
    return {
        "id": gid,
        "friendlyName": {"value": {"text": name}},
        "memberDevices": {
            "items": [
                {"id": mid, "friendlyNameObject": {"value": {"text": mname}}}
                for mid, mname in members
            ]
        },
    }


def test_group_rows_counts_members_and_names():
    raw = [_gql_group(
        "amzn1.alexa.endpointGroup.g1", "Downstairs",
        [("amzn1.alexa.endpoint.a", "Lamp"),
         ("amzn1.alexa.endpoint.b", "TV"),
         ("amzn1.alexa.endpoint.c", "Fan")],
    )]
    rows = groups.group_rows(raw)
    assert rows[0]["id"] == "amzn1.alexa.endpointGroup.g1"
    assert rows[0]["name"] == "Downstairs"
    assert rows[0]["members"] == 3
    assert rows[0]["memberNames"] == ["Lamp", "TV", "Fan"]


def test_normalize_name_is_case_space_punct_insensitive():
    assert groups.normalize_name("Living Room") == "livingroom"
    assert groups.normalize_name("living-room!") == "livingroom"
    assert groups.normalize_name("  LIVING   ROOM  ") == "livingroom"
    assert groups.normalize_name("") == ""


def test_find_group_by_id_and_name():
    autos = [
        _gql_group("amzn1.alexa.endpointGroup.g1", "Living Room", []),
        _gql_group("amzn1.alexa.endpointGroup.g2", "Kitchen", []),
    ]
    assert groups.find_group(autos, "amzn1.alexa.endpointGroup.g2")["friendlyName"]["value"]["text"] == "Kitchen"
    # normalized name lookup (case/space insensitive)
    assert groups.find_group(autos, "living room")["id"] == "amzn1.alexa.endpointGroup.g1"
    assert groups.find_group(autos, "LivingRoom")["id"] == "amzn1.alexa.endpointGroup.g1"
    assert groups.find_group(autos, "nope") is None
    assert groups.find_group(autos, "") is None


def test_endpoint_map_resolves_ha_entities():
    items = [
        {"id": "amzn1.alexa.endpoint.e1",
         "legacyAppliance": {"applianceId": "AAA_light#kitchen_lamp"}},
        {"id": "amzn1.alexa.endpoint.e2",
         "legacyAppliance": {"applianceId": "BBB_switch#barista_machine_power"}},
        # non-HA endpoint (no parseable tail) — skipped
        {"id": "amzn1.alexa.endpoint.e3",
         "legacyAppliance": {"applianceId": "hue-native-id"}},
    ]
    m = groups.endpoint_map(items)
    assert m["light.kitchen_lamp"] == "amzn1.alexa.endpoint.e1"
    assert m["switch.barista_machine_power"] == "amzn1.alexa.endpoint.e2"
    assert "hue-native-id" not in m and len(m) == 2


def test_resolve_members_maps_entities_passes_endpoints_dedupes():
    ent_map = {"light.kitchen_lamp": "amzn1.alexa.endpoint.e1"}
    resolved, unresolved = groups.resolve_members(
        ["light.kitchen_lamp", "light.missing"],
        ["amzn1.alexa.endpoint.e1", "amzn1.alexa.endpoint.x"],
        ent_map,
    )
    # e1 came from the entity AND was passed as an endpoint -> de-duped
    assert resolved == ["amzn1.alexa.endpoint.e1", "amzn1.alexa.endpoint.x"]
    assert unresolved == ["light.missing"]


# ── groups GraphQL variables builders (the gotchas) ────────────────────

def test_create_variables_lists_are_real_arrays_no_unit_ids():
    v = groups.build_create_variables("Den", ["amzn1.alexa.endpoint.a",
                                              "amzn1.alexa.endpoint.b"])
    inp = v["in"]
    assert inp["friendlyName"] == "Den"
    # CRITICAL: must be a real list, not a json.dumps'd string (silent no-op)
    assert isinstance(inp["memberDeviceIds"], list)
    assert inp["memberDeviceIds"] == ["amzn1.alexa.endpoint.a", "amzn1.alexa.endpoint.b"]
    # CRITICAL: create must NOT carry associatedUnitIds (-> BAD_REQUEST)
    assert "associatedUnitIds" not in inp
    # and it survives JSON serialization as an array, not a string
    assert isinstance(json.loads(json.dumps(v))["in"]["memberDeviceIds"], list)


def test_update_variables_operation_and_real_list():
    v = groups.build_update_variables(
        "amzn1.alexa.endpointGroup.g1", ["amzn1.alexa.endpoint.a"], "add")
    inp = v["in"]
    assert inp["deviceGroupId"] == "amzn1.alexa.endpointGroup.g1"
    assert inp["memberDeviceIdsUpdateOperation"] == "ADD"
    assert isinstance(inp["memberDeviceIds"], list)
    assert inp["memberDeviceIds"] == ["amzn1.alexa.endpoint.a"]


def test_update_variables_rejects_bad_operation():
    import pytest
    with pytest.raises(ValueError):
        groups.build_update_variables("g1", [], "FROB")


def test_delete_variables():
    v = groups.build_delete_variables("amzn1.alexa.endpointGroup.g1")
    assert v == {"in": {"deviceGroupId": "amzn1.alexa.endpointGroup.g1"}}


# ── session helpers ────────────────────────────────────────────────────

def test_cookie_filename():
    assert session.cookie_filename("a@b.com") == "alexa_media.a@b.com.pickle"


def test_make_outputpath_joins(tmp_path):
    op = session.make_outputpath(tmp_path)
    assert op("alexa_media.x.pickle") == str(tmp_path / "alexa_media.x.pickle")


def test_base_url():
    assert session.base_url("amazon.co.uk") == "https://alexa.amazon.co.uk"
    assert session.base_url("amazon.com") == "https://alexa.amazon.com"


def test_import_pickle_copies_and_renames(tmp_path):
    src = tmp_path / "src.pickle"
    src.write_bytes(b"cookie-bytes")
    dest_dir = tmp_path / "cfg"
    dest = session.import_pickle(src, "a@b.com", config_dir=dest_dir)
    assert dest == dest_dir / "alexa_media.a@b.com.pickle"
    assert dest.read_bytes() == b"cookie-bytes"


def test_import_pickle_missing_raises(tmp_path):
    import pytest
    with pytest.raises(session.AlexaSessionError):
        session.import_pickle(tmp_path / "nope.pickle", "a@b.com",
                              config_dir=tmp_path / "cfg")
