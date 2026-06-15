"""Unit tests for bulk / pattern rename + DACS speakable-name validation.

Pure logic only — no alexapy, no network. Covers sed parsing (delimiters,
i/g flags, capture groups), the real-world example patterns, no-op skipping,
--map file parsing, speakable_name + the pre-validation predicate, and the
DACS-error detection used to translate API rejections into friendly hints.
"""

import json

import pytest

from cli_anything.alexa.core import endpoints


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


def _records(names):
    raw = [
        _item(f"amzn1.alexa.endpoint.e{i}", f"SKILL_blob_sensor#s{i}",
              "Home Assistant", n)
        for i, n in enumerate(names)
    ]
    return endpoints.endpoint_records(raw)


# ── sed parsing ────────────────────────────────────────────────────────

def test_parse_sed_basic():
    rx, repl, is_global = endpoints.parse_sed("s/a/b/")
    assert rx.pattern == "a" and repl == "b" and is_global is False


def test_parse_sed_global_and_ignorecase_flags():
    rx, repl, is_global = endpoints.parse_sed("s/foo/bar/gi")
    assert is_global is True
    import re
    assert rx.flags & re.IGNORECASE


def test_parse_sed_alternate_delimiter():
    rx, repl, _ = endpoints.parse_sed("s|x|y|")
    assert rx.pattern == "x" and repl == "y"


def test_parse_sed_escaped_delimiter():
    # s/a\/b/c/ -> regex "a/b" replacement "c"
    rx, repl, _ = endpoints.parse_sed(r"s/a\/b/c/")
    assert rx.pattern == "a/b" and repl == "c"


def test_parse_sed_rejects_non_substitution():
    with pytest.raises(endpoints.PatternError):
        endpoints.parse_sed("not a pattern")
    with pytest.raises(endpoints.PatternError):
        endpoints.parse_sed("s/a/")          # too few parts
    with pytest.raises(endpoints.PatternError):
        endpoints.parse_sed("s/a/b/z")       # bad flag
    with pytest.raises(endpoints.PatternError):
        endpoints.parse_sed("s/(/b/")        # bad regex


def test_apply_sed_capture_group():
    assert endpoints.apply_sed(r"s/^Spots - (.*)/\1 Spots/", "Spots - Kitchen") \
        == "Kitchen Spots"


def test_apply_sed_global_vs_first_only():
    assert endpoints.apply_sed("s/a/X/", "banana") == "bXnana"
    assert endpoints.apply_sed("s/a/X/g", "banana") == "bXnXnX"


# ── the proven real-world example patterns ──────────────────────────────

def test_example_spots_pattern():
    recs = _records(["Spots - Kitchen", "Spots - Hall", "Lamp"])
    planned = endpoints.plan_pattern_renames(recs, r"s/^Spots - (.*)/\1 Spots/")
    pairs = {(p["old"], p["new"]) for p in planned}
    assert pairs == {("Spots - Kitchen", "Kitchen Spots"),
                     ("Spots - Hall", "Hall Spots")}
    assert all(p["old"] != p["new"] for p in planned)


def test_example_strip_prefix():
    recs = _records(["TH - Office", "TH - Den", "Kitchen"])
    planned = endpoints.plan_pattern_renames(recs, "s/^TH - //")
    assert {(p["old"], p["new"]) for p in planned} == \
        {("TH - Office", "Office"), ("TH - Den", "Den")}


def test_example_sensor_temperature_suffix():
    recs = _records(["Office Sensor Temperature", "Den Temperature"])
    planned = endpoints.plan_pattern_renames(
        recs, "s/ Sensor Temperature$/ Temperature/")
    # only the first changes; the second is a no-op (already ends correctly)
    assert len(planned) == 1
    assert planned[0]["new"] == "Office Temperature"


def test_example_optional_group_radiator():
    recs = _records(["Radiator - Hall Temperature", "Radiator - Bath"])
    planned = endpoints.plan_pattern_renames(
        recs, r"s/^Radiator - (.*?)( Temperature)?$/\1 Radiator/")
    new_by_old = {p["old"]: p["new"] for p in planned}
    assert new_by_old["Radiator - Hall Temperature"] == "Hall Radiator"
    assert new_by_old["Radiator - Bath"] == "Bath Radiator"


def test_pattern_skips_noops():
    recs = _records(["Already Fine", "x"])
    # a pattern that matches nothing -> no planned renames
    assert endpoints.plan_pattern_renames(recs, "s/ZZZ/QQQ/") == []


# ── --map file parsing ──────────────────────────────────────────────────

def test_parse_rename_map_name_and_id_with_comments():
    text = (
        "# a comment line\n"
        "Old Name => New Name\n"
        "\n"
        "amzn1.alexa.endpoint.e1 => Renamed Endpoint  \n"
        "  Spaced Left => Spaced Right \n"
    )
    pairs = endpoints.parse_rename_map(text)
    assert pairs == [
        ("Old Name", "New Name"),
        ("amzn1.alexa.endpoint.e1", "Renamed Endpoint"),
        ("Spaced Left", "Spaced Right"),
    ]


def test_parse_rename_map_rejects_bad_lines():
    with pytest.raises(ValueError):
        endpoints.parse_rename_map("no arrow here\n")
    with pytest.raises(ValueError):
        endpoints.parse_rename_map(" => empty target\n")


def test_plan_map_renames_resolves_and_reports_problems():
    recs = _records(["Kitchen Spots", "Hall Lamp"])
    eid0 = recs[0]["endpointId"]
    pairs = [
        ("Kitchen Spots", "Kitchen Ceiling"),       # by name
        (eid0, "Kitchen Ceiling 2"),                # by endpoint id (same dev)
        ("No Such Device", "X"),                    # unresolved
        ("Hall Lamp", "Hall Lamp"),                 # no-op -> skipped
    ]
    planned, problems = endpoints.plan_map_renames(recs, pairs)
    news = {p["new"] for p in planned}
    assert "Kitchen Ceiling" in news and "Kitchen Ceiling 2" in news
    assert all(p["new"] != "Hall Lamp" for p in planned)   # no-op skipped
    assert len(problems) == 1 and problems[0]["reason"] == "no match"


# ── speakable_name / DACS pre-validation ────────────────────────────────

def test_speakable_name_hyphens_to_spaces():
    assert endpoints.speakable_name("elt-k8s-1 Temperature") == "elt k8s 1 Temperature"


def test_speakable_name_strips_control_chars_and_collapses_ws():
    assert endpoints.speakable_name("Den\x05  Lamp") == "Den Lamp"
    assert endpoints.speakable_name("  a -  b ") == "a b"
    assert endpoints.speakable_name("") == ""


def test_is_speakable_predicate():
    assert endpoints.is_speakable("elt k8s 1 Temperature") is True
    assert endpoints.is_speakable("elt-k8s-1") is False
    assert endpoints.is_speakable("Den\x05Lamp") is False
    assert endpoints.is_speakable(None) is True


def test_speakable_warning_message():
    w = endpoints.speakable_warning("elt-k8s-1")
    assert w is not None
    assert "elt-k8s-1" in w and "elt k8s 1" in w
    assert endpoints.speakable_warning("elt k8s 1") is None


def test_pattern_rename_speakable_flag_applies_transform():
    recs = _records(["elt-k8s-1 Sensor Temperature"])
    planned = endpoints.plan_pattern_renames(
        recs, "s/ Sensor Temperature$/ Temperature/", speakable=True)
    assert planned[0]["new"] == "elt k8s 1 Temperature"
    assert planned[0]["warning"] is None


def test_pattern_rename_without_speakable_warns():
    recs = _records(["elt-k8s-1 Sensor Temperature"])
    planned = endpoints.plan_pattern_renames(
        recs, "s/ Sensor Temperature$/ Temperature/")
    assert planned[0]["new"] == "elt-k8s-1 Temperature"
    assert planned[0]["warning"] and "non-speakable" in planned[0]["warning"]


def test_is_dacs_error_detection():
    assert endpoints.is_dacs_error(
        'Invalid input. Invalid input from DACS (errorCode BAD_REQUEST)') is True
    assert endpoints.is_dacs_error("some BAD_REQUEST thing") is True
    assert endpoints.is_dacs_error("network timeout") is False


def test_planned_entries_json_serializable():
    recs = _records(["Spots - Kitchen"])
    planned = endpoints.plan_pattern_renames(recs, r"s/^Spots - (.*)/\1 Spots/")
    assert json.loads(json.dumps(planned)) == planned
