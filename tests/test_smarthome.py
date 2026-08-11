"""Tests for cli_anything.alexa.core.smarthome — the phoenix state read/control layer.

Two halves, matching the module:

* the **pure** helpers (value normalisation, light-change planning, entity
  addressing, payload flattening, Guard detection) — exercised directly;
* the thin **async** wrappers — exercised with `alexapy.AlexaAPI` patched, so no
  network and no real account is involved.  Those tests assert on the arguments
  handed to alexapy, because that boundary is exactly where the harness has been
  bitten before (raw dict vs DeviceRef, 0-100 vs 0.0-1.0).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import smarthome


def _run(coro):
    return asyncio.run(coro)


# ── normalize_brightness ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (100, 100), ("42", 42), (55.4, 55), (55.6, 56)],
)
def test_normalize_brightness_accepts_in_range(value, expected):
    assert smarthome.normalize_brightness(value) == expected


@pytest.mark.parametrize("value", [-1, 101, "abc", None, float("nan"), float("inf")])
def test_normalize_brightness_rejects_bad_values(value):
    """Out-of-range/NaN/inf must raise — set_light_state would silently drop them."""
    with pytest.raises(ValueError, match="brightness"):
        smarthome.normalize_brightness(value)


# ── colour normalisation ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [("Sky Blue", "sky_blue"), ("sky-blue", "sky_blue"), ("  RED  ", "red"), ("sky  blue", "sky_blue")],
)
def test_normalize_color_accepts_human_spellings(given, expected):
    assert smarthome.normalize_color(given) == expected


def test_normalize_color_rejects_unknown_and_lists_palette():
    with pytest.raises(ValueError, match="unknown colour") as exc:
        smarthome.normalize_color("burnt sienna")
    assert "crimson" in str(exc.value)


def test_normalize_color_rejects_empty():
    with pytest.raises(ValueError, match="required"):
        smarthome.normalize_color("  ")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("Warm White", "warm_white"), ("cool-white", "cool_white"), ("white", "white")],
)
def test_normalize_color_temperature_accepts_human_spellings(given, expected):
    assert smarthome.normalize_color_temperature(given) == expected


def test_normalize_color_temperature_rejects_unknown():
    with pytest.raises(ValueError, match="unknown colour temperature"):
        smarthome.normalize_color_temperature("2700k")


def test_normalize_color_temperature_rejects_empty():
    with pytest.raises(ValueError, match="required"):
        smarthome.normalize_color_temperature("")


# ── normalize_guard_state ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("away", "ARMED_AWAY"),
        ("home", "ARMED_STAY"),
        ("HOME", "ARMED_STAY"),
        ("ARMED_AWAY", "ARMED_AWAY"),
        ("armed_stay", "ARMED_STAY"),
    ],
)
def test_normalize_guard_state(given, expected):
    assert smarthome.normalize_guard_state(given) == expected


def test_normalize_guard_state_rejects_disarmed():
    """There is no DISARMED arm state — `home` is how Guard stands down."""
    with pytest.raises(ValueError, match="unknown guard state"):
        smarthome.normalize_guard_state("disarmed")


# ── plan_light_change ────────────────────────────────────────────────────


def test_plan_light_change_orders_actions_as_alexa_applies_them():
    plan = smarthome.plan_light_change(power=True, brightness="60", color="Sky Blue")
    assert plan["actions"] == ["turnOn", "setBrightness=60", "setColor=sky_blue"]
    assert plan["brightness"] == 60
    assert plan["color"] == "sky_blue"
    assert plan["color_temperature"] is None


def test_plan_light_change_power_off_only():
    plan = smarthome.plan_light_change(power=False)
    assert plan["actions"] == ["turnOff"]
    assert plan["power"] is False


def test_plan_light_change_color_temperature_only():
    plan = smarthome.plan_light_change(color_temperature="warm white")
    assert plan["actions"] == ["setColorTemperature=warm_white"]
    assert plan["power"] is None


def test_plan_light_change_rejects_color_plus_color_temperature():
    with pytest.raises(ValueError, match="mutually exclusive"):
        smarthome.plan_light_change(color="red", color_temperature="warm_white")


def test_plan_light_change_rejects_empty_request():
    """An empty controlRequests list returns 200 having done nothing."""
    with pytest.raises(ValueError, match="nothing to change"):
        smarthome.plan_light_change()


def test_plan_light_change_propagates_validation_errors():
    with pytest.raises(ValueError, match="brightness"):
        smarthome.plan_light_change(brightness=500)


# ── entity addressing ────────────────────────────────────────────────────


def test_entity_ref_returns_entity_id():
    assert smarthome.entity_ref({"entityId": "abc-123", "name": "Lamp"}) == "abc-123"


def test_entity_ref_refuses_record_without_entity_id_and_names_it():
    with pytest.raises(ValueError, match="Lamp") as exc:
        smarthome.entity_ref({"entityId": "", "name": "Lamp"})
    assert "entityId" in str(exc.value)


def test_entity_ref_falls_back_to_appliance_id_in_message():
    with pytest.raises(ValueError, match="APPL-1"):
        smarthome.entity_ref({"applianceId": "APPL-1"})


def test_entity_ref_handles_empty_record():
    with pytest.raises(ValueError, match="device"):
        smarthome.entity_ref({})


def test_entity_refs_skips_records_without_entity_id():
    records = [{"entityId": "a"}, {"entityId": ""}, {"name": "no id"}, {"entityId": "b"}]
    assert smarthome.entity_refs(records) == ["a", "b"]


def test_entity_refs_handles_none():
    assert smarthome.entity_refs(None) == []


def test_name_by_entity_maps_ids_to_names():
    records = [{"entityId": "a", "name": "Lamp"}, {"entityId": "b"}, {"name": "orphan"}]
    assert smarthome.name_by_entity(records) == {"a": "Lamp"}


# ── state_rows ───────────────────────────────────────────────────────────


def _state_payload(entity_id="e1", capabilities=None, errors=None):
    """A realistic /api/phoenix/state body: capabilityStates are JSON *strings*."""
    caps = capabilities if capabilities is not None else [
        {"namespace": "Alexa.PowerController", "name": "powerState", "value": "ON"},
        {"namespace": "Alexa.BrightnessController", "name": "brightness", "value": 75},
    ]
    body = {
        "deviceStates": [
            {
                "entity": {"entityId": entity_id, "entityType": "ENTITY"},
                "capabilityStates": [json.dumps(c) for c in caps],
            }
        ]
    }
    if errors is not None:
        body["errors"] = errors
    return body


def test_state_rows_decodes_json_encoded_capability_strings():
    rows = smarthome.state_rows(_state_payload())
    assert [(r["capability"], r["property"], r["value"]) for r in rows] == [
        ("PowerController", "powerState", "ON"),
        ("BrightnessController", "brightness", 75),
    ]
    assert all(r["entityId"] == "e1" for r in rows)


def test_state_rows_labels_rows_with_display_name():
    rows = smarthome.state_rows(_state_payload(), [{"entityId": "e1", "name": "Kitchen Lamp"}])
    assert {r["name"] for r in rows} == {"Kitchen Lamp"}


def test_state_rows_accepts_already_decoded_capability_dicts():
    payload = {
        "deviceStates": [
            {
                "entity": {"entityId": "e1"},
                "capabilityStates": [
                    {"namespace": "Alexa.PowerController", "name": "powerState", "value": "OFF"}
                ],
            }
        ]
    }
    assert smarthome.state_rows(payload)[0]["value"] == "OFF"


def test_state_rows_skips_undecodable_capability_blobs():
    payload = {
        "deviceStates": [
            {"entity": {"entityId": "e1"}, "capabilityStates": ["not json", 7, None]}
        ]
    }
    assert smarthome.state_rows(payload) == []


def test_state_rows_skips_non_dict_device_states():
    assert smarthome.state_rows({"deviceStates": ["junk", None]}) == []


def test_state_rows_keeps_non_alexa_namespace_verbatim():
    payload = {
        "deviceStates": [
            {
                "entity": {"entityId": "e1"},
                "capabilityStates": [{"namespace": "Custom.Thing", "name": "x", "value": 1}],
            }
        ]
    }
    assert smarthome.state_rows(payload)[0]["capability"] == "Custom.Thing"


def test_state_rows_tolerates_missing_namespace_and_entity():
    payload = {"deviceStates": [{"capabilityStates": [{"name": "x", "value": 1}]}]}
    row = smarthome.state_rows(payload)[0]
    assert row["capability"] is None
    assert row["entityId"] is None


@pytest.mark.parametrize("payload", [None, [], "junk", {}])
def test_state_rows_returns_empty_for_unusable_payloads(payload):
    assert smarthome.state_rows(payload) == []


# ── state_errors ─────────────────────────────────────────────────────────


def test_state_errors_flattens_unreachable_devices():
    payload = _state_payload(
        errors=[{"entity": {"entityId": "e9"}, "code": "ENDPOINT_UNREACHABLE"}]
    )
    assert smarthome.state_errors(payload) == [
        {"entityId": "e9", "code": "ENDPOINT_UNREACHABLE", "message": None}
    ]


def test_state_errors_accepts_alternate_field_names():
    payload = {"errors": [{"errorCode": "BAD", "description": "nope"}]}
    assert smarthome.state_errors(payload) == [
        {"entityId": None, "code": "BAD", "message": "nope"}
    ]


def test_state_errors_skips_non_dict_entries_and_bad_payloads():
    assert smarthome.state_errors({"errors": ["oops", None]}) == []
    assert smarthome.state_errors("junk") == []
    assert smarthome.state_errors({}) == []


# ── power_state ──────────────────────────────────────────────────────────


def test_power_state_reads_the_power_capability():
    assert smarthome.power_state(_state_payload()) == "ON"


def test_power_state_scoped_to_entity_id_ignores_other_devices():
    assert smarthome.power_state(_state_payload(entity_id="e1"), entity_id="other") is None


def test_power_state_none_when_device_has_no_power_capability():
    payload = _state_payload(
        capabilities=[{"namespace": "Alexa.BrightnessController", "name": "brightness", "value": 5}]
    )
    assert smarthome.power_state(payload) is None


def test_power_state_ignores_non_string_values():
    payload = _state_payload(
        capabilities=[{"namespace": "Alexa.PowerController", "name": "powerState", "value": {"x": 1}}]
    )
    assert smarthome.power_state(payload) is None


# ── find_guard / guard_row ───────────────────────────────────────────────


def test_find_guard_matches_security_panel_appliance_type():
    records = [
        {"name": "Lamp", "applianceTypes": ["LIGHT"]},
        {"name": "Guard Panel", "applianceTypes": ["SECURITY_PANEL"]},
    ]
    assert smarthome.find_guard(records)["name"] == "Guard Panel"


def test_find_guard_accepts_a_bare_string_appliance_type():
    assert smarthome.find_guard([{"name": "P", "applianceTypes": "security_panel"}])["name"] == "P"


def test_find_guard_falls_back_to_the_name_when_types_are_absent():
    records = [{"name": "Lamp"}, {"name": "Alexa Guard"}]
    assert smarthome.find_guard(records)["name"] == "Alexa Guard"


def test_find_guard_returns_none_when_absent():
    assert smarthome.find_guard([{"name": "Lamp", "applianceTypes": ["LIGHT"]}]) is None
    assert smarthome.find_guard(None) is None


def test_guard_row_maps_arm_state_back_to_the_human_verb():
    payload = _state_payload(
        capabilities=[
            {"namespace": "Alexa.SecurityPanelController", "name": "armState", "value": "ARMED_AWAY"}
        ]
    )
    assert smarthome.guard_row(payload, name="Guard") == {
        "name": "Guard",
        "armState": "ARMED_AWAY",
        "mode": "away",
    }


def test_guard_row_handles_the_securitypanelstate_property_name():
    payload = _state_payload(
        capabilities=[{"namespace": "Alexa.X", "name": "securityPanelState", "value": "ARMED_STAY"}]
    )
    assert smarthome.guard_row(payload)["mode"] == "home"


def test_guard_row_unknown_state_has_no_mode():
    payload = _state_payload(
        capabilities=[{"namespace": "Alexa.X", "name": "armState", "value": "WEIRD"}]
    )
    row = smarthome.guard_row(payload)
    assert row["armState"] == "WEIRD"
    assert row["mode"] is None


def test_guard_row_empty_payload():
    assert smarthome.guard_row({}) == {"name": None, "armState": None, "mode": None}


# ── fetch_states / read_states (async) ───────────────────────────────────


def test_fetch_states_passes_entity_ids_to_alexapy():
    captured = {}

    async def _fake(login, entity_ids=None, appliance_ids=None):
        captured.update(entity_ids=entity_ids, appliance_ids=appliance_ids)
        return _state_payload()

    with patch("alexapy.AlexaAPI.get_entity_state", new=_fake):
        payload = _run(smarthome.fetch_states(MagicMock(), entity_ids=["e1"]))
    assert captured == {"entity_ids": ["e1"], "appliance_ids": None}
    assert payload["deviceStates"]


def test_fetch_states_passes_appliance_ids_to_alexapy():
    captured = {}

    async def _fake(login, entity_ids=None, appliance_ids=None):
        captured.update(entity_ids=entity_ids, appliance_ids=appliance_ids)
        return {}

    with patch("alexapy.AlexaAPI.get_entity_state", new=_fake):
        _run(smarthome.fetch_states(MagicMock(), appliance_ids=["A1"]))
    assert captured == {"entity_ids": None, "appliance_ids": ["A1"]}


def test_fetch_states_requires_at_least_one_id():
    with pytest.raises(ValueError, match="nothing to read"):
        _run(smarthome.fetch_states(MagicMock()))


def test_fetch_states_normalises_a_none_response_to_an_empty_dict():
    with patch("alexapy.AlexaAPI.get_entity_state", new=AsyncMock(return_value=None)):
        assert _run(smarthome.fetch_states(MagicMock(), entity_ids=["e1"])) == {}


def test_read_states_returns_rows_errors_and_skipped():
    records = [
        {"entityId": "e1", "name": "Kitchen Lamp"},
        {"entityId": "", "name": "Unaddressable"},
    ]
    payload = _state_payload(errors=[{"entity": {"entityId": "e1"}, "code": "UNREACHABLE"}])
    with patch("alexapy.AlexaAPI.get_entity_state", new=AsyncMock(return_value=payload)):
        result = _run(smarthome.read_states(MagicMock(), records))
    assert [r["property"] for r in result["states"]] == ["powerState", "brightness"]
    assert result["states"][0]["name"] == "Kitchen Lamp"
    assert result["errors"][0]["code"] == "UNREACHABLE"
    assert result["skipped"] == ["Unaddressable"]


def test_read_states_raises_when_no_record_is_addressable():
    with pytest.raises(ValueError, match="none of the selected devices"):
        _run(smarthome.read_states(MagicMock(), [{"name": "Lamp"}]))


# ── set_light_state / set_power (async) ──────────────────────────────────


def test_set_light_state_forwards_normalised_values_to_alexapy():
    captured = {}

    async def _fake(login, entity_id, power_on=True, brightness=None, color_name=None,
                    color_temperature_name=None):
        captured.update(
            entity_id=entity_id,
            power_on=power_on,
            brightness=brightness,
            color_name=color_name,
            color_temperature_name=color_temperature_name,
        )
        return {"controlResponses": []}

    with patch("alexapy.AlexaAPI.set_light_state", new=_fake):
        result = _run(
            smarthome.set_light_state(
                MagicMock(), "e1", power=True, brightness="80", color="Sky Blue"
            )
        )
    assert captured == {
        "entity_id": "e1",
        "power_on": True,
        "brightness": 80,
        "color_name": "sky_blue",
        "color_temperature_name": None,
    }
    assert result["actions"] == ["turnOn", "setBrightness=80", "setColor=sky_blue"]
    assert result["entityId"] == "e1"


def test_set_light_state_defaults_power_on_when_only_brightness_given():
    """A brightness-only change must still send turnOn — alexapy always sends one."""
    captured = {}

    async def _fake(login, entity_id, power_on=True, **kwargs):
        captured["power_on"] = power_on
        return None

    with patch("alexapy.AlexaAPI.set_light_state", new=_fake):
        result = _run(smarthome.set_light_state(MagicMock(), "e1", brightness=30))
    assert captured["power_on"] is True
    assert result["actions"] == ["setBrightness=30"]
    assert result["response"] == {}


def test_set_light_state_validates_before_calling_the_api():
    api = AsyncMock()
    with patch("alexapy.AlexaAPI.set_light_state", new=api):
        with pytest.raises(ValueError, match="mutually exclusive"):
            _run(
                smarthome.set_light_state(
                    MagicMock(), "e1", color="red", color_temperature="warm_white"
                )
            )
    api.assert_not_awaited()


@pytest.mark.parametrize(("on", "expected"), [(True, "turnOn"), (False, "turnOff")])
def test_set_power_sends_only_the_power_verb(on, expected):
    captured = {}

    async def _fake(login, entity_id, power_on=True, brightness=None, color_name=None,
                    color_temperature_name=None):
        captured.update(power_on=power_on, brightness=brightness, color_name=color_name)
        return {}

    with patch("alexapy.AlexaAPI.set_light_state", new=_fake):
        result = _run(smarthome.set_power(MagicMock(), "e1", on))
    assert captured == {"power_on": on, "brightness": None, "color_name": None}
    assert result["actions"] == [expected]


# ── guard (async) ────────────────────────────────────────────────────────


def test_fetch_guard_state_reads_by_appliance_id():
    captured = {}

    async def _fake(login, appliance_id):
        captured["appliance_id"] = appliance_id
        return _state_payload(
            capabilities=[{"namespace": "Alexa.X", "name": "armState", "value": "ARMED_STAY"}]
        )

    with patch("alexapy.AlexaAPI.get_guard_state", new=_fake):
        row = _run(smarthome.fetch_guard_state(MagicMock(), "APPL-GUARD", name="Guard"))
    assert captured["appliance_id"] == "APPL-GUARD"
    assert row == {"name": "Guard", "armState": "ARMED_STAY", "mode": "home"}


def test_fetch_guard_state_handles_none_response():
    with patch("alexapy.AlexaAPI.get_guard_state", new=AsyncMock(return_value=None)):
        assert _run(smarthome.fetch_guard_state(MagicMock(), "A"))["armState"] is None


def test_set_guard_state_sends_the_mapped_arm_state():
    captured = {}

    async def _fake(login, entity_id, state):
        captured.update(entity_id=entity_id, state=state)
        return {"controlResponses": [{"code": "SUCCESS"}]}

    with patch("alexapy.AlexaAPI.static_set_guard_state", new=_fake):
        result = _run(smarthome.set_guard_state(MagicMock(), "e-guard", "away", name="Guard"))
    assert captured == {"entity_id": "e-guard", "state": "ARMED_AWAY"}
    assert result["armState"] == "ARMED_AWAY"
    assert result["name"] == "Guard"


def test_set_guard_state_validates_before_calling_the_api():
    api = AsyncMock()
    with patch("alexapy.AlexaAPI.static_set_guard_state", new=api):
        with pytest.raises(ValueError, match="unknown guard state"):
            _run(smarthome.set_guard_state(MagicMock(), "e", "disarmed"))
    api.assert_not_awaited()
