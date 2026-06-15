"""Unit tests for nested / child-group support + native-delete warning/verify.

Pure logic only. Covers the child-group variables builders (asserting real
arrays, NOT json.dumps'd strings, and the childDeviceGroupIdsUpdateOperation
enum), child-group name->id resolution, group_rows surfacing child groups, the
native-source delete-warning predicate, and the delete-verify "reappeared" diff.
"""

import json

import pytest

from cli_anything.alexa.core import groups, endpoints


# ── child-group variables builders ──────────────────────────────────────

def test_create_variables_with_child_groups_real_array():
    v = groups.build_create_variables(
        "Downstairs", [],
        child_group_ids=["amzn1.alexa.endpointGroup.r1",
                         "amzn1.alexa.endpointGroup.r2"])
    inp = v["in"]
    # CRITICAL: real list, not a json.dumps'd string (a lone string no-ops)
    assert isinstance(inp["childDeviceGroupIds"], list)
    assert inp["childDeviceGroupIds"] == ["amzn1.alexa.endpointGroup.r1",
                                          "amzn1.alexa.endpointGroup.r2"]
    # survives JSON serialization as an array
    assert isinstance(json.loads(json.dumps(v))["in"]["childDeviceGroupIds"], list)
    # never carries associatedUnitIds
    assert "associatedUnitIds" not in inp


def test_create_variables_omits_child_field_when_none():
    v = groups.build_create_variables("Den", ["amzn1.alexa.endpoint.a"])
    assert "childDeviceGroupIds" not in v["in"]


def test_update_variables_with_child_groups_uses_enum_op():
    v = groups.build_update_variables(
        "amzn1.alexa.endpointGroup.g1", [], "add",
        child_group_ids=["amzn1.alexa.endpointGroup.r1"])
    inp = v["in"]
    assert isinstance(inp["childDeviceGroupIds"], list)
    assert inp["childDeviceGroupIds"] == ["amzn1.alexa.endpointGroup.r1"]
    # the enum op MUST be present and upper-cased
    assert inp["childDeviceGroupIdsUpdateOperation"] == "ADD"
    # member op still present too (REPLACE/ADD/REMOVE shared)
    assert inp["memberDeviceIdsUpdateOperation"] == "ADD"


def test_update_variables_omits_child_op_when_none():
    v = groups.build_update_variables(
        "amzn1.alexa.endpointGroup.g1", ["amzn1.alexa.endpoint.a"], "replace")
    assert "childDeviceGroupIds" not in v["in"]
    assert "childDeviceGroupIdsUpdateOperation" not in v["in"]


def test_update_variables_child_replace():
    v = groups.build_update_variables(
        "g1", [], "replace", child_group_ids=["c1", "c2"])
    assert v["in"]["childDeviceGroupIdsUpdateOperation"] == "REPLACE"


# ── child-group name->id resolution ─────────────────────────────────────

def _gql_group(gid, name, children=None):
    rec = {
        "id": gid,
        "friendlyName": {"value": {"text": name}},
        "memberDevices": {"items": []},
    }
    if children:
        rec["childDeviceGroups"] = [
            {"id": cid, "friendlyName": {"value": {"text": cname}}}
            for cid, cname in children
        ]
    return rec


def test_resolve_child_groups_by_name_and_id_dedupes():
    raw = [
        _gql_group("amzn1.alexa.endpointGroup.r1", "Living Room"),
        _gql_group("amzn1.alexa.endpointGroup.r2", "Kitchen"),
    ]
    ids, unresolved = groups.resolve_child_groups(
        raw, ["living room", "amzn1.alexa.endpointGroup.r1", "Kitchen"])
    # "living room" and the explicit r1 id are the same group -> de-duped
    assert ids == ["amzn1.alexa.endpointGroup.r1", "amzn1.alexa.endpointGroup.r2"]
    assert unresolved == []


def test_resolve_child_groups_reports_unresolved():
    raw = [_gql_group("amzn1.alexa.endpointGroup.r1", "Living Room")]
    ids, unresolved = groups.resolve_child_groups(raw, ["Living Room", "Nope"])
    assert ids == ["amzn1.alexa.endpointGroup.r1"]
    assert unresolved == ["Nope"]


# ── group_rows surfaces child groups ────────────────────────────────────

def test_group_rows_includes_child_groups():
    raw = [_gql_group(
        "amzn1.alexa.endpointGroup.g1", "Downstairs",
        children=[("amzn1.alexa.endpointGroup.r1", "Living Room"),
                  ("amzn1.alexa.endpointGroup.r2", "Kitchen")])]
    row = groups.group_rows(raw)[0]
    assert row["childGroups"] == 2
    assert row["childGroupNames"] == ["Living Room", "Kitchen"]


def test_group_rows_no_child_groups_defaults_zero():
    raw = [_gql_group("amzn1.alexa.endpointGroup.g1", "Bare")]
    row = groups.group_rows(raw)[0]
    assert row["childGroups"] == 0 and row["childGroupNames"] == []


# ── native-source delete warning ────────────────────────────────────────

def _rec(name, manufacturer, ha=False):
    return {
        "endpointId": "amzn1.alexa.endpoint.x",
        "applianceId": "aid-" + name,
        "name": name,
        "manufacturer": manufacturer,
        "ha_sourced": ha,
        "entity_id": None,
    }


def test_native_delete_warning_for_native_device():
    w = endpoints.native_delete_warning(_rec("Light Bar", "Signify Netherlands B.V."))
    assert w is not None
    assert "Light Bar" in w and "Hue" in w and "re-sync" in w


def test_native_delete_warning_tuya_source():
    w = endpoints.native_delete_warning(_rec("Plug", "Tuya"))
    assert "Smart Life" in w


def test_native_delete_warning_none_for_ha():
    assert endpoints.native_delete_warning(
        _rec("Kitchen Light", "Home Assistant", ha=True)) is None


def test_native_source_hint_fallback():
    assert "source app" in endpoints.native_source_hint("Acme Widgets")
    assert "source app" in endpoints.native_source_hint(None)


# ── delete-verify "reappeared" diff ─────────────────────────────────────

def _after(name, appliance_id):
    return {"name": name, "applianceId": appliance_id, "ha_sourced": False}


def test_reappeared_by_appliance_id():
    deleted = [{"applianceId": "aid-1", "name": "Plug"}]
    after = [_after("Plug", "aid-1"), _after("Other", "aid-2")]
    rea = endpoints.reappeared_after_delete(deleted, after)
    assert len(rea) == 1 and rea[0]["reappeared_as"] == "applianceId"


def test_reappeared_by_name_when_id_changed():
    # native re-sync can give a NEW applianceId but the same name
    deleted = [{"applianceId": "old-id", "name": "Light Bar"}]
    after = [_after("light bar", "brand-new-id")]
    rea = endpoints.reappeared_after_delete(deleted, after)
    assert len(rea) == 1 and rea[0]["reappeared_as"] == "name"


def test_not_reappeared_when_gone():
    deleted = [{"applianceId": "aid-1", "name": "Plug"}]
    after = [_after("Other", "aid-2")]
    assert endpoints.reappeared_after_delete(deleted, after) == []
    assert endpoints.reappeared_after_delete([], after) == []
