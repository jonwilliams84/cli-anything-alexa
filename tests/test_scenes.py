"""Tests for cli_anything.alexa.core.scenes — the smart-home scene surface.

Two halves, matching the module:

* the **pure** helpers (scene detection, row flattening, the `Activate`
  controlRequest wire format, the response-derived three-valued `ok`);
* the thin **async** wrappers — exercised with the network faked
  (`endpoints.fetch_endpoint_records` for the read, `AlexaAPI._static_request`
  for the write), because the boundary where this harness has been bitten
  before is exactly at the request payload.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import scenes


def _run(coro):
    return asyncio.run(coro)


# ── is_scene ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "types",
    [["SCENE"], ["SMARTPLAY"], ["LIGHT", "SCENE"], ["scene"], ("SCENE",)],
)
def test_is_scene_accepts_scene_appliance_types(types):
    assert scenes.is_scene({"applianceTypes": types}) is True


@pytest.mark.parametrize(
    "record",
    [
        {"applianceTypes": ["LIGHT"]},
        {"applianceTypes": []},
        {"applianceTypes": None},
        {},
        {"applianceTypes": "SCENE"},  # string form is tolerated, still a scene
        "not-a-dict",
        None,
    ],
)
def test_is_scene_is_type_safe(record):
    assert scenes.is_scene(record) in (True, False)


def test_is_scene_string_appliance_type_is_detected():
    assert scenes.is_scene({"applianceTypes": "SMARTPLAY"}) is True


# ── scene_rows ───────────────────────────────────────────────────────────

_RECORDS = [
    {
        "endpointId": "amzn1.alexa.endpoint.movie",
        "applianceId": "amzn1.alexa.scene.movie",
        "entityId": "entity-movie",
        "applianceTypes": ["SCENE"],
        "name": "Movie Night",
        "manufacturer": "Amazon",
        "ha_sourced": False,
        "entity_id": None,
        "enabled": "ENABLED",
    },
    {
        "endpointId": "amzn1.alexa.endpoint.goodnight",
        "applianceId": "SKILL_scene#goodnight",
        "entityId": "entity-goodnight",
        "applianceTypes": ["SCENE"],
        "name": "Good Night",
        "manufacturer": "Home Assistant",
        "ha_sourced": True,
        "entity_id": "scene.goodnight",
        "enabled": "ENABLED",
    },
    {
        "endpointId": "amzn1.alexa.endpoint.lamp",
        "applianceId": "SKILL_light#kitchen_lamp",
        "entityId": "entity-lamp",
        "applianceTypes": ["LIGHT"],
        "name": "Kitchen Lamp",
        "manufacturer": "Home Assistant",
        "ha_sourced": True,
        "entity_id": "light.kitchen_lamp",
        "enabled": "ENABLED",
    },
    {
        "endpointId": "amzn1.alexa.endpoint.broken",
        "applianceId": "APPL-BROKEN",
        "entityId": "",
        "applianceTypes": ["SMARTPLAY"],
        "name": "Broken Scene",
        "manufacturer": "Tuya",
        "ha_sourced": False,
        "entity_id": None,
        "enabled": "ENABLED",
    },
]


def test_scene_rows_filters_and_sorts_by_name():
    rows = scenes.scene_rows(_RECORDS)
    assert [r["name"] for r in rows] == ["Broken Scene", "Good Night", "Movie Night"]
    assert "Kitchen Lamp" not in [r["name"] for r in rows]


def test_scene_rows_shape():
    rows = scenes.scene_rows(_RECORDS)
    movie = next(r for r in rows if r["name"] == "Movie Night")
    assert movie == {
        "name": "Movie Night",
        "entityId": "entity-movie",
        "applianceId": "amzn1.alexa.scene.movie",
        "endpointId": "amzn1.alexa.endpoint.movie",
        "manufacturer": "Amazon",
        "source": "native",
    }
    goodnight = next(r for r in rows if r["name"] == "Good Night")
    assert goodnight["source"] == "HA"


def test_scene_rows_keeps_entityless_scenes_visible_but_unactivatable():
    broken = next(r for r in scenes.scene_rows(_RECORDS) if r["name"] == "Broken Scene")
    assert broken["entityId"] is None


def test_scene_rows_empty_and_junk_safe():
    assert scenes.scene_rows([]) == []
    assert scenes.scene_rows(None) == []
    assert scenes.scene_rows(["junk", 42]) == []


@pytest.mark.parametrize("types", [["SCENE"], ["SMARTPLAY"], ["scene"]])
def test_scene_rows_detects_both_scene_markers(types):
    rows = scenes.scene_rows(
        [{"applianceTypes": types, "name": "S", "entityId": "e", "applianceId": "a"}]
    )
    assert len(rows) == 1


# ── scene_control_request ────────────────────────────────────────────────


def test_scene_control_request_is_the_app_wire_format():
    assert scenes.scene_control_request("entity-movie") == {
        "controlRequests": [
            {
                "entityId": "entity-movie",
                "entityType": "ENTITY",
                "parameters": {"action": "Activate"},
            }
        ]
    }


# ── activate_verify ──────────────────────────────────────────────────────


def test_activate_verify_true_when_every_code_is_success():
    body = {"controlResponses": [{"code": "SUCCESS"}]}
    assert scenes.activate_verify(body) is True


def test_activate_verify_false_on_a_failure_code():
    body = {"controlResponses": [{"code": "SUCCESS"}, {"code": "NOENT"}]}
    assert scenes.activate_verify(body) is False
    assert scenes.activate_verify({"controlResponses": [{"code": "internal_error"}]}) is False


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        [],
        "nope",
        {"controlResponses": []},
        {"controlResponses": None},
        {"controlResponses": ["junk", 42]},
    ],
)
def test_activate_verify_none_when_the_response_answers_nothing(response):
    assert scenes.activate_verify(response) is None


def test_activate_verify_codes_are_case_insensitive():
    assert scenes.activate_verify({"controlResponses": [{"code": "success"}]}) is True


# ── fetch_scenes ─────────────────────────────────────────────────────────


def test_fetch_scenes_filters_the_endpoint_records():
    async def _fake(login):
        return list(_RECORDS)

    with patch(
        "cli_anything.alexa.core.endpoints.fetch_endpoint_records", new=_fake
    ):
        found = _run(scenes.fetch_scenes(MagicMock()))
    assert [r["name"] for r in found] == ["Movie Night", "Good Night", "Broken Scene"]


# ── activate_scene ───────────────────────────────────────────────────────


def test_activate_scene_puts_the_app_wire_format():
    captured = {}

    class _Resp:
        status = 200

        @staticmethod
        async def text():
            return json.dumps({"controlResponses": [{"code": "SUCCESS"}]})

    async def _fake(method, login, path, data=None, **kwargs):
        captured.update(method=method, path=path, data=data)
        return _Resp()

    with patch("alexapy.AlexaAPI._static_request", new=_fake):
        result = _run(scenes.activate_scene(MagicMock(), "entity-movie", name="Movie Night"))
    assert captured["method"] == "put"
    assert captured["path"] == "/api/phoenix/state"
    assert captured["data"] == scenes.scene_control_request("entity-movie")
    assert result["ok"] is True
    assert result["action"] == "Activate"
    assert result["name"] == "Movie Night"
    assert result["entityId"] == "entity-movie"


def test_activate_scene_survives_a_non_json_response():
    class _Resp:
        @staticmethod
        async def text():
            return "<html>oops</html>"

    with patch("alexapy.AlexaAPI._static_request", new=AsyncMock(return_value=_Resp())):
        result = _run(scenes.activate_scene(MagicMock(), "e1"))
    assert result["response"] == {}
    assert result["ok"] is None


def test_activate_scene_raises_when_the_response_is_missing():
    with patch("alexapy.AlexaAPI._static_request", new=AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="no response"):
            _run(scenes.activate_scene(MagicMock(), "e1"))
