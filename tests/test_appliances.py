"""Unit tests for the pure appliance logic — no alexapy, no network."""

from cli_anything.alexa.core import appliances


# ── parse_entity_id ────────────────────────────────────────────────────

def test_parse_entity_id_basic():
    aid = "amzn1.alexa.appliance.AAABBB_light#kitchen_lamp"
    assert appliances.parse_entity_id(aid) == "light.kitchen_lamp"


def test_parse_entity_id_switch_with_underscores_in_object():
    aid = "amzn1.alexa.appliance.XYZ_switch#barista_machine_power"
    assert appliances.parse_entity_id(aid) == "switch.barista_machine_power"


def test_parse_entity_id_sensor():
    aid = "PREFIX_sensor#living_room_temperature"
    assert appliances.parse_entity_id(aid) == "sensor.living_room_temperature"


def test_parse_entity_id_no_hash_returns_none():
    assert appliances.parse_entity_id("amzn1.native.hue.lamp01") is None


def test_parse_entity_id_empty():
    assert appliances.parse_entity_id("") is None
    assert appliances.parse_entity_id(None) is None


def test_parse_entity_id_malformed_hash_only():
    # a hash but no domain/object
    assert appliances.parse_entity_id("foo_#bar") is None
    assert appliances.parse_entity_id("foo_light#") is None


# ── is_ha_sourced ──────────────────────────────────────────────────────

def test_is_ha_sourced_true():
    assert appliances.is_ha_sourced({"manufacturerName": "Home Assistant"})


def test_is_ha_sourced_false():
    assert not appliances.is_ha_sourced({"manufacturerName": "Philips"})
    assert not appliances.is_ha_sourced({})


# ── appliance_row ──────────────────────────────────────────────────────

def test_appliance_row_ha():
    raw = {
        "applianceId": "p_light#kitchen",
        "friendlyName": "Kitchen Lamp",
        "manufacturerName": "Home Assistant",
        "modelName": "Light",
    }
    row = appliances.appliance_row(raw)
    assert row["ha_sourced"] is True
    assert row["entity_id"] == "light.kitchen"
    assert row["friendlyName"] == "Kitchen Lamp"


def test_appliance_row_native_has_no_entity():
    raw = {
        "applianceId": "p_light#kitchen",  # parseable shape...
        "manufacturerName": "Philips",      # ...but not HA-sourced
    }
    row = appliances.appliance_row(raw)
    assert row["ha_sourced"] is False
    assert row["entity_id"] is None


# ── load_whitelist ─────────────────────────────────────────────────────

def test_load_whitelist_parses_and_strips():
    text = """
    # heading comment
    light.kitchen
    switch.barista   # inline note
       sensor.temp

    # trailing comment
    """
    wl = appliances.load_whitelist(text)
    assert wl == {"light.kitchen", "switch.barista", "sensor.temp"}


def test_load_whitelist_empty():
    assert appliances.load_whitelist("") == set()
    assert appliances.load_whitelist(None) == set()


# ── plan_prune ─────────────────────────────────────────────────────────

def _ha(entity_tail):
    return {"applianceId": f"p_{entity_tail}", "manufacturerName": "Home Assistant"}


def test_plan_prune_deletes_non_whitelisted_ha():
    appliances_list = [
        _ha("light#kitchen"),     # whitelisted -> keep
        _ha("light#orphan"),      # not whitelisted -> delete
        _ha("switch#barista"),    # whitelisted -> keep
        {"applianceId": "native_hue", "manufacturerName": "Philips"},  # skip
    ]
    wl = {"light.kitchen", "switch.barista"}
    plan = appliances.plan_prune(appliances_list, wl)

    delete_ids = {r["applianceId"] for r in plan["delete"]}
    keep_ids = {r["applianceId"] for r in plan["keep"]}
    skip_ids = {r["applianceId"] for r in plan["skipped"]}

    assert delete_ids == {"p_light#orphan"}
    assert keep_ids == {"p_light#kitchen", "p_switch#barista"}
    assert skip_ids == {"native_hue"}


def test_plan_prune_unparseable_ha_is_deleted():
    # HA-sourced but no decodable entity -> treated as orphan -> delete
    bad = {"applianceId": "no_hash_here", "manufacturerName": "Home Assistant"}
    plan = appliances.plan_prune([bad], {"light.kitchen"})
    assert plan["delete"] and plan["delete"][0]["applianceId"] == "no_hash_here"
    assert not plan["keep"]


def test_plan_prune_empty():
    plan = appliances.plan_prune([], {"light.kitchen"})
    assert plan == {"delete": [], "keep": [], "skipped": []}
