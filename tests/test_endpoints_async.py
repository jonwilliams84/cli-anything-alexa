"""Tests for the async network layer in endpoints.py — _graphql, fetch_*,
rename_endpoint, apply_renames — plus uncovered branches in the pure
resolve_target / find_duplicates helpers.

All async functions are exercised with mocked alexapy so no network or
real account is needed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_anything.alexa.core import endpoints


def _run(coro):
    return asyncio.run(coro)


def _mock_response(body_dict):
    """Build a mock aiohttp-like response whose .text() returns JSON."""
    resp = MagicMock()
    resp.text = AsyncMock(return_value=json.dumps(body_dict))
    return resp


# ── _graphql ─────────────────────────────────────────────────────────────

def test_graphql_raises_on_errors_field():
    """_graphql raises RuntimeError when the response carries GraphQL errors."""
    mock_login = MagicMock()
    error_body = {"errors": [{"message": "Field 'foo' is invalid"}]}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(error_body))):
        with pytest.raises(RuntimeError, match="GraphQL error"):
            _run(endpoints._graphql(mock_login, "query { foo }"))


def test_graphql_passes_variables_through():
    """_graphql includes variables in the request data when provided."""
    mock_login = MagicMock()
    ok_body = {"data": {"endpoints": {"items": []}}}
    captured = {}

    async def _capture(*args, **kwargs):
        captured["data"] = kwargs.get("data")
        return _mock_response(ok_body)

    with patch("alexapy.AlexaAPI._static_request", new=_capture):
        _run(endpoints._graphql(mock_login, "query", variables={"in": {"x": 1}}))

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
        _run(endpoints._graphql(mock_login, "query", variables=None))

    assert "variables" not in captured["data"]


# ── fetch_endpoints / fetch_endpoint_records ────────────────────────────

def _item(eid, appliance_id, manufacturer, display):
    return {
        "id": eid,
        "legacyAppliance": {
            "applianceId": appliance_id,
            "manufacturerName": manufacturer,
            "friendlyName": display,
        },
        "friendlyNameObject": {"value": {"text": display}},
        "enablement": "ENABLED",
    }


def test_fetch_endpoints_extracts_items():
    """fetch_endpoints returns the raw items list from the GraphQL response."""
    mock_login = MagicMock()
    items = [_item("amzn1.alexa.endpoint.x", "SKILL_blob_light#k", "Home Assistant", "Kitchen")]
    body = {"data": {"endpoints": {"items": items}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(endpoints.fetch_endpoints(mock_login))
    assert result == items


def test_fetch_endpoints_empty_when_data_missing():
    """fetch_endpoints returns [] when data/endpoints/items is absent."""
    mock_login = MagicMock()
    body = {"data": {}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(endpoints.fetch_endpoints(mock_login))
    assert result == []


def test_fetch_endpoint_records_flattens():
    """fetch_endpoint_records returns flattened records with entity_id decoded."""
    mock_login = MagicMock()
    items = [_item("amzn1.alexa.endpoint.h1", "SKILL_blob_switch#lounge", "Home Assistant", "Lounge")]
    body = {"data": {"endpoints": {"items": items}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        records = _run(endpoints.fetch_endpoint_records(mock_login))
    assert len(records) == 1
    assert records[0]["entity_id"] == "switch.lounge"
    assert records[0]["name"] == "Lounge"


# ── rename_endpoint ─────────────────────────────────────────────────────

def test_rename_endpoint_success():
    """rename_endpoint returns endpointId, friendlyName, and the mutation result."""
    mock_login = MagicMock()
    body = {"data": {"setEndpointFriendlyName": {"ok": True}}}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        result = _run(endpoints.rename_endpoint(mock_login, "amzn1.alexa.endpoint.x", "Kitchen"))
    assert result["endpointId"] == "amzn1.alexa.endpoint.x"
    assert result["friendlyName"] == "Kitchen"
    assert result["result"] == {"ok": True}


def test_rename_endpoint_dacs_error_raises_value_error_with_warning():
    """A DACS rejection is re-raised as ValueError with the speakable warning."""
    mock_login = MagicMock()
    error_body = {"errors": [{"message": "DACS validation failed: bad_request"}]}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(error_body))):
        with pytest.raises(ValueError, match="speakable"):
            _run(endpoints.rename_endpoint(mock_login, "amzn1.alexa.endpoint.x", "elt-k8s-prod"))


def test_rename_endpoint_non_dacs_runtime_error_re_raised():
    """A non-DACS RuntimeError is re-raised as-is, not converted to ValueError."""
    mock_login = MagicMock()
    error_body = {"errors": [{"message": "Internal server error"}]}
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(error_body))):
        with pytest.raises(RuntimeError, match="GraphQL error"):
            _run(endpoints.rename_endpoint(mock_login, "amzn1.alexa.endpoint.x", "Kitchen"))


# ── apply_renames ────────────────────────────────────────────────────────

def test_apply_renames_success_path():
    """A valid entry produces ok=True with the mutation result."""
    mock_login = MagicMock()
    body = {"data": {"setEndpointFriendlyName": {"ok": True}}}
    planned = [{"old": "A", "new": "B", "endpointId": "amzn1.alexa.endpoint.x"}]
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(body))):
        results = _run(endpoints.apply_renames(mock_login, planned))
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["result"] == {"ok": True}


def test_apply_renames_dacs_error_captured_per_entry():
    """A DACS rejection on one entry is captured, not raised, so the batch continues."""
    mock_login = MagicMock()
    error_body = {"errors": [{"message": "DACS bad_request"}]}
    planned = [{"old": "A", "new": "elt-k8s", "endpointId": "amzn1.alexa.endpoint.x"}]
    with patch("alexapy.AlexaAPI._static_request",
               new=AsyncMock(return_value=_mock_response(error_body))):
        results = _run(endpoints.apply_renames(mock_login, planned))
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "speakable" in results[0]["error"]


def test_apply_renames_none_plan_returns_empty():
    """apply_renames(None) returns [] without calling the network."""
    results = _run(endpoints.apply_renames(MagicMock(), None))
    assert results == []


# ── resolve_target: normalized-name branch with empty normalized target ──

def test_resolve_target_empty_normalized_name_skips_normalized_search():
    """When normalize_name(target) is empty, the normalized branch is skipped."""
    recs = [{"endpointId": "x", "name": "Kitchen", "applianceId": "a",
             "manufacturer": "HA", "ha_sourced": False, "entity_id": None,
             "enabled": "ENABLED"}]
    # A target that normalizes to empty (e.g. just punctuation) should not
    # match via the normalized-name branch.
    assert endpoints.resolve_target(recs, "!!!") == []


# ── find_duplicates: second record with same normalized name ─────────────

def test_find_duplicates_appends_to_existing_normalized_name():
    """When a second record shares a normalized name, it's appended to the
    existing list rather than creating a new entry — the `if norm not in
    by_name` false branch."""
    recs = [
        {"endpointId": "e1", "name": "Kitchen Light", "applianceId": "a1",
         "manufacturer": "HA", "ha_sourced": True, "entity_id": "light.kitchen",
         "enabled": "ENABLED"},
        {"endpointId": "e2", "name": "kitchen light", "applianceId": "a2",
         "manufacturer": "Belkin", "ha_sourced": False, "entity_id": None,
         "enabled": "ENABLED"},
    ]
    dups = endpoints.find_duplicates(recs)
    # Both records share the same normalized name → one group with two entries
    assert len(dups) == 1
    assert len(dups[0]["endpoints"]) == 2
    assert {r["endpointId"] for r in dups[0]["endpoints"]} == {"e1", "e2"}
