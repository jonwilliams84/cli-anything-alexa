"""Unit tests for the pure endpoint logic — no alexapy, no network.

Covers target resolution (applianceId vs endpoint id vs exact vs normalized
name + the ambiguity path), the --entity / --name resolvers, duplicate
detection, device-row filtering, and the setEndpointFriendlyName variables
builder.
"""

import json

from cli_anything.alexa.core import endpoints


# ── fixtures ────────────────────────────────────────────────────────────

def _item(eid, appliance_id, manufacturer, display, enablement="ENABLED"):
    """Build a raw `endpoints` query item."""
    return {
        "id": eid,
        "legacyAppliance": {
            "applianceId": appliance_id,
            "manufacturerName": manufacturer,
            "friendlyName": display,
        },
        "friendlyNameObject": {"value": {"text": display}},
        "enablement": enablement,
    }


# a native Wemo plug + an HA twin sharing the display name "Lounge Plug",
# plus a unique HA light.
_RAW = [
    _item("amzn1.alexa.endpoint.native1",
          "AAA_SonarCloudService_uuid:Socket-1_0-XYZ", "Belkin International Inc.",
          "Lounge Plug"),
    _item("amzn1.alexa.endpoint.ha1",
          "SKILL_blob_switch#lounge_plug", "Home Assistant", "Lounge Plug"),
    _item("amzn1.alexa.endpoint.ha2",
          "SKILL_blob_light#kitchen_big", "Home Assistant", "Kitchen Spots"),
]
_RECORDS = endpoints.endpoint_records(_RAW)


# ── endpoint_record / records ───────────────────────────────────────────

def test_endpoint_record_ha_decodes_entity():
    r = endpoints.endpoint_record(_RAW[1])
    assert r["endpointId"] == "amzn1.alexa.endpoint.ha1"
    assert r["applianceId"] == "SKILL_blob_switch#lounge_plug"
    assert r["name"] == "Lounge Plug"
    assert r["manufacturer"] == "Home Assistant"
    assert r["ha_sourced"] is True
    assert r["entity_id"] == "switch.lounge_plug"
    assert r["enabled"] == "ENABLED"


def test_endpoint_record_native_has_no_entity():
    r = endpoints.endpoint_record(_RAW[0])
    assert r["ha_sourced"] is False
    assert r["entity_id"] is None
    assert r["manufacturer"] == "Belkin International Inc."


def test_endpoint_record_display_name_falls_back_to_legacy():
    raw = {
        "id": "amzn1.alexa.endpoint.x",
        "legacyAppliance": {"applianceId": "n", "manufacturerName": "Tuya",
                            "friendlyName": "Fallback Name"},
        "friendlyNameObject": {"value": {}},
    }
    assert endpoints.endpoint_record(raw)["name"] == "Fallback Name"


# ── resolve_target (precedence + ambiguity) ─────────────────────────────

def test_resolve_target_exact_appliance_id():
    hits = endpoints.resolve_target(_RECORDS, "SKILL_blob_light#kitchen_big")
    assert len(hits) == 1
    assert hits[0]["endpointId"] == "amzn1.alexa.endpoint.ha2"


def test_resolve_target_exact_endpoint_id():
    hits = endpoints.resolve_target(_RECORDS, "amzn1.alexa.endpoint.native1")
    assert len(hits) == 1 and hits[0]["manufacturer"] == "Belkin International Inc."


def test_resolve_target_exact_display_name_unique():
    hits = endpoints.resolve_target(_RECORDS, "Kitchen Spots")
    assert len(hits) == 1 and hits[0]["entity_id"] == "light.kitchen_big"


def test_resolve_target_normalized_name_unique():
    # case/space-insensitive match on the unique device
    hits = endpoints.resolve_target(_RECORDS, "kitchenspots")
    assert len(hits) == 1 and hits[0]["endpointId"] == "amzn1.alexa.endpoint.ha2"


def test_resolve_target_ambiguous_name_returns_both():
    # native + HA twin share the display name -> AMBIGUOUS (two matches).
    hits = endpoints.resolve_target(_RECORDS, "Lounge Plug")
    assert len(hits) == 2
    sources = {("HA" if h["ha_sourced"] else "native") for h in hits}
    assert sources == {"HA", "native"}


def test_resolve_target_ambiguous_normalized_name():
    hits = endpoints.resolve_target(_RECORDS, "  lounge-plug! ")
    assert len(hits) == 2


def test_resolve_target_no_match():
    assert endpoints.resolve_target(_RECORDS, "nonexistent") == []
    assert endpoints.resolve_target(_RECORDS, "") == []


def test_resolve_target_appliance_id_beats_name():
    # exact applianceId tier wins even though a name tier would also match.
    hits = endpoints.resolve_target(_RECORDS, "SKILL_blob_switch#lounge_plug")
    assert len(hits) == 1 and hits[0]["endpointId"] == "amzn1.alexa.endpoint.ha1"


# ── resolve_by_entity / resolve_by_name ─────────────────────────────────

def test_resolve_by_entity():
    hits = endpoints.resolve_by_entity(_RECORDS, "switch.lounge_plug")
    assert len(hits) == 1 and hits[0]["endpointId"] == "amzn1.alexa.endpoint.ha1"
    assert endpoints.resolve_by_entity(_RECORDS, "switch.nope") == []
    assert endpoints.resolve_by_entity(_RECORDS, "") == []


def test_resolve_by_name_targets_native():
    # name->endpoint, normalized; the ambiguous "Lounge Plug" returns both,
    # while the unique one returns the native/HA pair as needed.
    hits = endpoints.resolve_by_name(_RECORDS, "Kitchen Spots")
    assert len(hits) == 1 and hits[0]["endpointId"] == "amzn1.alexa.endpoint.ha2"
    amb = endpoints.resolve_by_name(_RECORDS, "lounge plug")
    assert len(amb) == 2
    assert endpoints.resolve_by_name(_RECORDS, "") == []


# ── ambiguous_matches descriptor ────────────────────────────────────────

def test_ambiguous_matches_descriptor():
    amb = endpoints.resolve_target(_RECORDS, "Lounge Plug")
    desc = endpoints.ambiguous_matches(amb)
    assert {d["source"] for d in desc} == {"HA", "native"}
    assert all("endpointId" in d and "applianceId" in d for d in desc)


# ── duplicate detection ─────────────────────────────────────────────────

def test_find_duplicates_native_plus_ha_twin():
    dups = endpoints.find_duplicates(_RECORDS)
    assert len(dups) == 1
    d = dups[0]
    assert d["name"] == "Lounge Plug"
    assert d["count"] == 2
    assert d["native_plus_ha"] is True
    assert len(d["endpoints"]) == 2


def test_find_duplicates_two_native_same_name_not_twin():
    raw = [
        _item("e1", "n1", "Tuya", "Hallway"),
        _item("e2", "n2", "Tuya", "hallway"),  # normalized-equal, both native
    ]
    dups = endpoints.find_duplicates(endpoints.endpoint_records(raw))
    assert len(dups) == 1
    assert dups[0]["count"] == 2
    assert dups[0]["native_plus_ha"] is False


def test_find_duplicates_none():
    assert endpoints.find_duplicates(endpoints.endpoint_records([_RAW[2]])) == []
    assert endpoints.find_duplicates([]) == []


# ── device_rows filtering ───────────────────────────────────────────────

def test_device_rows_adds_source_and_columns():
    rows = endpoints.device_rows(_RECORDS)
    assert {r["source"] for r in rows} == {"HA", "native"}
    assert rows[0]["manufacturer"] == "Belkin International Inc."
    assert "endpointId" in rows[0] and "applianceId" in rows[0]


def test_device_rows_native_only():
    rows = endpoints.device_rows(_RECORDS, native_only=True)
    assert len(rows) == 1 and rows[0]["source"] == "native"


def test_device_rows_manufacturer_filter():
    rows = endpoints.device_rows(_RECORDS, manufacturer="belkin")
    assert len(rows) == 1 and rows[0]["manufacturer"] == "Belkin International Inc."
    assert endpoints.device_rows(_RECORDS, manufacturer="home assistant") and \
        all(r["source"] == "HA"
            for r in endpoints.device_rows(_RECORDS, manufacturer="home assistant"))


# ── rename variables builder ────────────────────────────────────────────

def test_build_rename_variables():
    v = endpoints.build_rename_variables("amzn1.alexa.endpoint.ha1", "New Name")
    assert v == {"in": {"endpointId": "amzn1.alexa.endpoint.ha1",
                        "friendlyName": "New Name"}}
    # survives JSON serialization unchanged
    assert json.loads(json.dumps(v)) == v
