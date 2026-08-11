"""Tests targeting previously uncovered logic in routines, project, and groups.

Focuses on error paths, edge cases, and branches that never executed:
  * routines._node_summary: @type fallback, targets (plural) key, non-str target
  * routines.action_targets: fallback to [start] when nodesToExecute is absent
  * routines.find_routine: utterance-only match (last-resort branch)
  * routines.run_routine: async error paths (no routine, no device)
  * routines.list_routines: async flattening
  * project.load_config: corrupt JSON swallowed, env override precedence
  * project.merge_cli_overrides: None values are not applied
  * project.save_config: chmod OSError is swallowed
  * groups async network layer: _graphql error raising, fetch/list/create/update/delete
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import groups, project, routines


def _run(coro):
    return asyncio.run(coro)


# ── routines._node_summary edge cases ────────────────────────────────────

def test_node_summary_falls_back_to_at_type_rsplit():
    """When 'type' is absent, the @type tail after the last dot is used."""
    node = {
        "@type": "com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode",
        "operationPayload": {"target": "dev-1", "operations": []},
    }
    summary = routines._node_summary(node)
    assert summary is not None
    assert "OpaquePayloadOperationNode" in summary
    assert "dev-1" in summary


def test_node_summary_uses_targets_plural_key():
    """payload.get('target') falls back to payload.get('targets')."""
    node = {
        "type": "Alexa.SmartHome.Batch",
        "operationPayload": {"targets": "group-abc"},
    }
    summary = routines._node_summary(node)
    assert summary is not None
    assert "group-abc" in summary


def test_node_summary_non_string_target_is_stringified():
    """A non-string target (e.g. a list) is str()'d, not crashed."""
    node = {
        "type": "Alexa.SmartHome.Batch",
        "operationPayload": {"target": ["a", "b"]},
    }
    summary = routines._node_summary(node)
    assert summary is not None
    assert "['a', 'b']" in summary


def test_node_summary_returns_none_for_empty_node():
    """A node with no type, no operations, and no target yields None."""
    assert routines._node_summary({}) is None
    assert routines._node_summary(None) is None


def test_node_summary_skips_non_dict_operations():
    """Non-dict entries in operations are skipped, not crashed on."""
    node = {
        "type": "Alexa.SmartHome.Batch",
        "operationPayload": {
            "operations": ["not-a-dict", {"type": "turnOn"}, None],
        },
    }
    summary = routines._node_summary(node)
    assert summary is not None
    assert "turnOn" in summary
    # the non-dict entries did not contribute a type
    assert "not-a-dict" not in summary


# ── routines.action_targets fallback to [start] ──────────────────────────

def test_action_targets_falls_back_to_start_node_when_no_nodes_to_execute():
    """When nodesToExecute is absent but startNode exists, startNode is used."""
    automation = {
        "sequence": {
            "startNode": {
                "type": "Alexa.SmartHome.Batch",
                "operationPayload": {
                    "target": "single-target",
                    "operations": [{"type": "turnOn"}],
                },
            }
        }
    }
    acts = routines.action_targets(automation)
    assert len(acts) == 1
    assert "single-target" in acts[0]


# ── routines.find_routine utterance-only match ───────────────────────────

def test_find_routine_matches_by_utterance_when_name_and_id_differ():
    """The last-resort branch matches the trigger utterance, not the name."""
    autos = [
        {
            "automationId": "amzn1.alexa.automation.42",
            "name": "Bedtime Protocol",
            "status": "ENABLED",
            "triggers": [{"payload": {"utterance": "sleep time"}}],
        }
    ]
    # "sleep time" is the utterance, not the name — only the last-resort
    # branch (walking routine_rows) can find it.
    result = routines.find_routine(autos, "sleep time")
    assert result is not None
    assert result["automationId"] == "amzn1.alexa.automation.42"


def test_find_routine_utterance_match_is_case_insensitive():
    autos = [
        {
            "automationId": "a99",
            "name": "X",
            "status": "ON",
            "triggers": [{"payload": {"utterance": "Good Morning"}}],
        }
    ]
    assert routines.find_routine(autos, "good morning")["automationId"] == "a99"


# ── routines.run_routine async error paths ───────────────────────────────

def test_run_routine_raises_when_no_routine_matches():
    """run_routine raises ValueError if find_routine returns None."""
    mock_login = MagicMock()
    with patch("alexapy.AlexaAPI.get_automations", new=AsyncMock(return_value=[])):
        with pytest.raises(ValueError, match="no routine matching"):
            _run(routines.run_routine(mock_login, "nonexistent"))


def test_run_routine_raises_when_no_device_available():
    """run_routine raises ValueError when no devices are returned."""
    mock_login = MagicMock()
    automations = [
        {
            "automationId": "a1",
            "name": "Test",
            "status": "ON",
            "triggers": [{"payload": {"utterance": "test"}}],
        }
    ]
    with patch("alexapy.AlexaAPI.get_automations", new=AsyncMock(return_value=automations)):
        with patch("alexapy.AlexaAPI.get_devices", new=AsyncMock(return_value=[])):
            with pytest.raises(ValueError, match="no Alexa device available"):
                _run(routines.run_routine(mock_login, "a1"))


def _patch_alexaapi(automations, devices, mock_api_instance):
    """Patch alexapy.AlexaAPI so static async methods + constructor all work."""
    mock_class = MagicMock()
    mock_class.get_automations = AsyncMock(return_value=automations)
    mock_class.get_devices = AsyncMock(return_value=devices)
    mock_class.return_value = mock_api_instance
    return patch("alexapy.AlexaAPI", new=mock_class)


def test_run_routine_picks_online_device_and_triggers():
    """run_routine prefers an online device and calls api.run_routine."""
    mock_login = MagicMock()
    automations = [
        {
            "automationId": "a1",
            "name": "Lights On",
            "status": "ON",
            "triggers": [{"payload": {"utterance": "lights on"}}],
        }
    ]
    devices = [
        {"serialNumber": "SN-OFF", "accountName": "Offline Echo", "online": False},
        {"serialNumber": "SN-KITCHEN", "accountName": "Kitchen Echo", "online": True},
    ]
    mock_api_instance = MagicMock()
    mock_api_instance.run_routine = AsyncMock()

    with _patch_alexaapi(automations, devices, mock_api_instance):
        result = _run(routines.run_routine(mock_login, "lights on"))

    assert result["triggered"] == "Lights On"
    assert result["via_device"] == "Kitchen Echo"
    assert result["utterance"] == "lights on"
    mock_api_instance.run_routine.assert_awaited_once_with("lights on")


def test_run_routine_falls_back_to_offline_device():
    """When no device is online, the first (offline) device is used."""
    mock_login = MagicMock()
    automations = [
        {
            "automationId": "a1",
            "name": "R",
            "status": "ON",
            "triggers": [{"payload": {"utterance": "run r"}}],
        }
    ]
    devices = [{"serialNumber": "SN-ONLY", "accountName": "Only Echo", "online": False}]
    mock_api_instance = MagicMock()
    mock_api_instance.run_routine = AsyncMock()

    with _patch_alexaapi(automations, devices, mock_api_instance):
        result = _run(routines.run_routine(mock_login, "run r"))

    assert result["via_device"] == "Only Echo"


def test_run_routine_falls_back_to_name_when_no_utterance():
    """When the routine has no trigger utterance, the name is used."""
    mock_login = MagicMock()
    automations = [
        {"automationId": "a1", "name": "Named Routine", "status": "ON", "triggers": []},
    ]
    devices = [{"serialNumber": "SN-ECHO", "accountName": "Echo", "online": True}]
    mock_api_instance = MagicMock()
    mock_api_instance.run_routine = AsyncMock()

    with _patch_alexaapi(automations, devices, mock_api_instance):
        result = _run(routines.run_routine(mock_login, "Named Routine"))

    assert result["utterance"] == "Named Routine"
    mock_api_instance.run_routine.assert_awaited_once_with("Named Routine")


def test_list_routines_returns_rows():
    """list_routines flattens raw automations into display rows."""
    mock_login = MagicMock()
    automations = [
        {"automationId": "a1", "name": "Test", "status": "ON",
         "triggers": [{"payload": {"utterance": "test"}}]},
    ]
    with patch("alexapy.AlexaAPI.get_automations", new=AsyncMock(return_value=automations)):
        rows = _run(routines.list_routines(mock_login))
    assert len(rows) == 1
    assert rows[0]["id"] == "a1"
    assert rows[0]["utterance"] == "test"


# ── project.load_config error paths ──────────────────────────────────────

def test_load_config_swallows_corrupt_json(tmp_path, monkeypatch):
    """A corrupt config file is silently ignored; defaults are returned."""
    monkeypatch.delenv("CLI_ALEXA_EMAIL", raising=False)
    monkeypatch.delenv("CLI_ALEXA_URL", raising=False)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ this is not valid json")
    loaded = project.load_config(cfg_path)
    assert loaded["email"] is None
    assert loaded["url"] == "amazon.co.uk"


def test_load_config_env_override_wins_over_file(tmp_path, monkeypatch):
    """CLI_ALEXA_* env vars override values stored in the config file."""
    cfg_path = tmp_path / "config.json"
    project.save_config({"email": "file@b.com", "url": "amazon.com"}, cfg_path)
    monkeypatch.setenv("CLI_ALEXA_EMAIL", "env@b.com")
    try:
        loaded = project.load_config(cfg_path)
        assert loaded["email"] == "env@b.com"
        assert loaded["url"] == "amazon.com"
    finally:
        monkeypatch.delenv("CLI_ALEXA_EMAIL", raising=False)


def test_load_config_returns_defaults_when_file_missing(tmp_path, monkeypatch):
    """No file on disk → pure defaults, no crash."""
    monkeypatch.delenv("CLI_ALEXA_EMAIL", raising=False)
    monkeypatch.delenv("CLI_ALEXA_URL", raising=False)
    loaded = project.load_config(tmp_path / "nonexistent.json")
    assert loaded == {"email": None, "url": "amazon.co.uk"}


# ── project.merge_cli_overrides ───────────────────────────────────────────

def test_merge_cli_overrides_applies_non_none_values():
    cfg = {"email": "old@b.com", "url": "amazon.co.uk"}
    merged = project.merge_cli_overrides(cfg, email="new@b.com", url="amazon.de")
    assert merged["email"] == "new@b.com"
    assert merged["url"] == "amazon.de"


def test_merge_cli_overrides_skips_none_values():
    """None kwargs must NOT overwrite existing config values."""
    cfg = {"email": "keep@b.com", "url": "amazon.co.uk"}
    merged = project.merge_cli_overrides(cfg, email=None, url="amazon.de")
    assert merged["email"] == "keep@b.com"
    assert merged["url"] == "amazon.de"


def test_merge_cli_overrides_does_not_mutate_input():
    cfg = {"email": "orig@b.com", "url": "amazon.co.uk"}
    merged = project.merge_cli_overrides(cfg, email="changed@b.com")
    assert cfg["email"] == "orig@b.com"
    assert merged["email"] == "changed@b.com"


# ── project.save_config chmod OSError ─────────────────────────────────────

def test_save_config_swallows_chmod_oserror(tmp_path, monkeypatch):
    """If os.chmod raises OSError (e.g. unsupported FS), save_config still succeeds."""
    cfg_path = tmp_path / "config.json"

    def _raise_oserror(_path, _mode):
        raise OSError("chmod not supported")

    monkeypatch.setattr(os, "chmod", _raise_oserror)
    result = project.save_config({"email": "x@y.com"}, cfg_path)
    assert result == cfg_path
    # file was still written
    assert json.loads(cfg_path.read_text())["email"] == "x@y.com"


# ── groups async network layer ───────────────────────────────────────────

def _mock_response(body_dict):
    """Build a mock aiohttp-like response whose .text() returns JSON."""
    resp = MagicMock()
    resp.text = AsyncMock(return_value=json.dumps(body_dict))
    return resp


def test_graphql_raises_on_errors_field():
    """_graphql raises RuntimeError when the response carries GraphQL errors."""
    mock_login = MagicMock()
    error_body = {"errors": [{"message": "Field 'foo' is invalid"}]}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(error_body))):
        with pytest.raises(RuntimeError, match="GraphQL error"):
            _run(groups._graphql(mock_login, "query { foo }"))


def test_graphql_passes_variables_through():
    """_graphql includes variables in the request data when provided."""
    mock_login = MagicMock()
    ok_body = {"data": {"listDeviceGroups": {"deviceGroups": []}}}
    captured = {}

    async def _capture(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return _mock_response(ok_body)

    with patch("alexapy.AlexaAPI._static_request", new=_capture):
        _run(groups._graphql(mock_login, "query", variables={"in": {"x": 1}}))

    assert captured["data"]["query"] == "query"
    assert captured["data"]["variables"] == {"in": {"x": 1}}


def test_graphql_omits_variables_when_none():
    """_graphql does not add a 'variables' key when variables is None."""
    mock_login = MagicMock()
    ok_body = {"data": {}}
    captured = {}

    async def _capture(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return _mock_response(ok_body)

    with patch("alexapy.AlexaAPI._static_request", new=_capture):
        _run(groups._graphql(mock_login, "query", variables=None))

    assert "variables" not in captured["data"]


def test_fetch_groups_extracts_device_groups():
    mock_login = MagicMock()
    body = {"data": {"listDeviceGroups": {"deviceGroups": [
        {"id": "g1", "friendlyName": {"value": {"text": "Living Room"}},
         "memberDevices": {"items": []}},
    ]}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.fetch_groups(mock_login))
    assert len(result) == 1
    assert result[0]["id"] == "g1"


def test_fetch_groups_empty_when_no_data():
    mock_login = MagicMock()
    body = {"data": {}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.fetch_groups(mock_login))
    assert result == []


def test_list_groups_returns_rows():
    mock_login = MagicMock()
    body = {"data": {"listDeviceGroups": {"deviceGroups": [
        {"id": "g1", "friendlyName": {"value": {"text": "Kitchen"}},
         "memberDevices": {"items": []}},
    ]}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        rows = _run(groups.list_groups(mock_login))
    assert len(rows) == 1
    assert rows[0]["name"] == "Kitchen"


def test_fetch_endpoint_map_builds_entity_to_endpoint():
    mock_login = MagicMock()
    body = {"data": {"endpoints": {"items": [
        {"id": "amzn1.alexa.endpoint.abc",
         "legacyAppliance": {"applianceId": "aid_light#kitchen"}},
    ]}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        ent_map = _run(groups.fetch_endpoint_map(mock_login))
    assert "light.kitchen" in ent_map
    assert ent_map["light.kitchen"] == "amzn1.alexa.endpoint.abc"


def test_create_group_returns_created_name_and_members():
    mock_login = MagicMock()
    body = {"data": {"createDeviceGroup": {"__typename": "DeviceGroup"}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.create_group(
            mock_login, "Den", ["amzn1.alexa.endpoint.a"]))
    assert result["created"] == "Den"
    assert result["memberDeviceIds"] == ["amzn1.alexa.endpoint.a"]
    assert result["result"] == {"__typename": "DeviceGroup"}
    assert "childDeviceGroupIds" not in result


def test_create_group_with_child_groups_includes_child_ids():
    mock_login = MagicMock()
    body = {"data": {"createDeviceGroup": {"__typename": "DeviceGroup"}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.create_group(
            mock_login, "Downstairs", [],
            child_group_ids=["amzn1.alexa.endpointGroup.r1"]))
    assert result["childDeviceGroupIds"] == ["amzn1.alexa.endpointGroup.r1"]


def test_update_group_returns_operation_and_members():
    mock_login = MagicMock()
    body = {"data": {"updateDeviceGroup": {"__typename": "DeviceGroup"}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.update_group(
            mock_login, "amzn1.alexa.endpointGroup.g1",
            ["amzn1.alexa.endpoint.a"], "add"))
    assert result["deviceGroupId"] == "amzn1.alexa.endpointGroup.g1"
    assert result["operation"] == "ADD"
    assert result["memberDeviceIds"] == ["amzn1.alexa.endpoint.a"]
    assert "childDeviceGroupIds" not in result


def test_update_group_with_child_groups_includes_child_op():
    mock_login = MagicMock()
    body = {"data": {"updateDeviceGroup": {"__typename": "DeviceGroup"}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.update_group(
            mock_login, "g1", [], "remove",
            child_group_ids=["amzn1.alexa.endpointGroup.r1"]))
    assert result["operation"] == "REMOVE"
    assert result["childDeviceGroupIds"] == ["amzn1.alexa.endpointGroup.r1"]


def test_delete_group_returns_group_id_and_result():
    mock_login = MagicMock()
    body = {"data": {"deleteDeviceGroup": {"__typename": "DeviceGroup"}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(groups.delete_group(mock_login, "amzn1.alexa.endpointGroup.g1"))
    assert result["deviceGroupId"] == "amzn1.alexa.endpointGroup.g1"
    assert result["result"] == {"__typename": "DeviceGroup"}
