"""Behavioural tests for the smart-home state/control CLI paths.

Covers `devices state`, `devices on/off`, `devices light` and the `guard` group.
Assertions are on observable behaviour — exit code, JSON on stdout, and which
core coroutine was invoked with what — never on source text.  Every mutating
command is checked against the harness-wide contract: **preview by default, act
only on --yes**.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import smarthome as smarthome_core

#: Two endpoint records: one addressable, one Guard panel.
_RECORDS = [
    {
        "endpointId": "amzn1.alexa.endpoint.lamp",
        "applianceId": "SKILL_blob_light#kitchen_lamp",
        "entityId": "entity-lamp",
        "applianceTypes": ["LIGHT"],
        "name": "Kitchen Lamp",
        "manufacturer": "Home Assistant",
        "ha_sourced": True,
        "entity_id": "light.kitchen_lamp",
        "enabled": "ENABLED",
    },
    {
        "endpointId": "amzn1.alexa.endpoint.plug",
        "applianceId": "APPL-PLUG",
        "entityId": "",
        "applianceTypes": ["SMARTPLUG"],
        "name": "Lounge Plug",
        "manufacturer": "Tuya",
        "ha_sourced": False,
        "entity_id": None,
        "enabled": "ENABLED",
    },
]

_GUARD = {
    "endpointId": "amzn1.alexa.endpoint.guard",
    "applianceId": "APPL-GUARD",
    "entityId": "entity-guard",
    "applianceTypes": ["SECURITY_PANEL"],
    "name": "Alexa Guard",
    "manufacturer": "Amazon",
    "ha_sourced": False,
    "entity_id": None,
    "enabled": "ENABLED",
}


@contextlib.contextmanager
def _stub_cli(records=None, results=None):
    """Patch `_login` and `_run` so no network/alexapy is involved.

    ``_run`` closes each coroutine it is handed (so nothing is left un-awaited)
    and answers `fetch_endpoint_records` with ``records``; every other call pops
    the next value off ``results``.  The mock is yielded so tests can assert on
    the coroutine names that were run.
    """
    queue = list(results or [])
    seen: list[str] = []

    def fake_run(_ctx, coro):
        name = getattr(coro, "__name__", None) or getattr(
            getattr(coro, "cr_code", None), "co_name", ""
        )
        seen.append(name)
        if hasattr(coro, "close"):
            coro.close()
        if name == "fetch_endpoint_records":
            return list(records if records is not None else _RECORDS)
        if name == "_as_coro":
            # the CLI routes the pure entity_ref validator through _run
            return "entity-lamp"
        return queue.pop(0) if queue else {}

    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock:
            mock.seen = seen
            yield mock


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_out(result):
    return json.loads(result.output)


# ── devices state ────────────────────────────────────────────────────────


def test_devices_state_reads_the_named_device():
    payload = {"states": [{"name": "Kitchen Lamp", "property": "powerState", "value": "ON"}],
               "errors": [], "skipped": []}
    with _stub_cli(results=[payload]) as run:
        result = _invoke(["--json", "devices", "state", "Kitchen Lamp"])
    assert result.exit_code == 0
    assert _json_out(result) == payload
    assert "read_states" in run.seen


def test_devices_state_accepts_an_appliance_id_target():
    with _stub_cli(results=[{"states": [], "errors": [], "skipped": []}]):
        result = _invoke(["--json", "devices", "state", "SKILL_blob_light#kitchen_lamp"])
    assert result.exit_code == 0


def test_devices_state_all_reads_every_record():
    captured = {}

    def fake_read(login, records):  # not a coroutine — recorded, never awaited
        captured["records"] = records
        return MagicMock(__name__="read_states")

    with _stub_cli(results=[{"states": [], "errors": [], "skipped": []}]):
        with patch.object(smarthome_core, "read_states", MagicMock(side_effect=fake_read)):
            result = _invoke(["--json", "devices", "state", "--all"])
    assert result.exit_code == 0
    assert [r["name"] for r in captured["records"]] == ["Kitchen Lamp", "Lounge Plug"]


def test_devices_state_rejects_all_combined_with_targets():
    with _stub_cli():
        result = _invoke(["devices", "state", "--all", "Kitchen Lamp"])
    assert result.exit_code == 1
    assert "--all cannot be combined" in result.output


def test_devices_state_requires_a_target():
    with _stub_cli():
        result = _invoke(["devices", "state"])
    assert result.exit_code == 1
    assert "name at least one device" in result.output


def test_devices_state_aborts_on_unknown_device():
    with _stub_cli():
        result = _invoke(["devices", "state", "Nope"])
    assert result.exit_code == 1
    assert "no device matching" in result.output


def test_devices_state_text_mode_reports_errors_and_skips_on_stderr():
    payload = {
        "states": [{"name": "Kitchen Lamp", "property": "powerState", "value": "ON"}],
        "errors": [{"entityId": "entity-lamp", "code": "ENDPOINT_UNREACHABLE"}],
        "skipped": ["Lounge Plug"],
    }
    with _stub_cli(results=[payload]):
        result = _invoke(["devices", "state", "Kitchen Lamp"])
    assert result.exit_code == 0
    assert "ENDPOINT_UNREACHABLE" in result.output
    assert "Lounge Plug" in result.output


def test_devices_state_ambiguous_name_aborts_with_candidates():
    twins = [dict(_RECORDS[0]), {**_RECORDS[0], "endpointId": "amzn1.alexa.endpoint.twin",
                                 "applianceId": "APPL-TWIN", "ha_sourced": False}]
    with _stub_cli(records=twins):
        result = _invoke(["devices", "state", "Kitchen Lamp"])
    assert result.exit_code == 1
    assert "matches 2 devices" in result.output


# ── devices on / off ─────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["on", "off"])
def test_devices_power_previews_without_yes(verb):
    with _stub_cli() as run:
        result = _invoke(["--json", "devices", verb, "Kitchen Lamp"])
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["action"] == ("turnOn" if verb == "on" else "turnOff")
    assert parsed["devices"] == ["Kitchen Lamp"]
    assert "re-run with --yes" in parsed["hint"]
    assert "set_power" not in run.seen


@pytest.mark.parametrize(("verb", "expected_on"), [("on", True), ("off", False)])
def test_devices_power_executes_with_yes(verb, expected_on):
    captured = {}

    def fake_set_power(login, entity_id, on):
        captured.update(entity_id=entity_id, on=on)
        return MagicMock(__name__="set_power")

    with _stub_cli(results=[{"entityId": "entity-lamp", "actions": ["turnOn"]}]):
        with patch.object(smarthome_core, "set_power", MagicMock(side_effect=fake_set_power)):
            result = _invoke(["--json", "devices", verb, "Kitchen Lamp", "--yes"])
    assert result.exit_code == 0
    assert captured == {"entity_id": "entity-lamp", "on": expected_on}
    assert _json_out(result)[0]["name"] == "Kitchen Lamp"


def test_devices_on_all_previews_every_device():
    with _stub_cli():
        result = _invoke(["--json", "devices", "on", "--all"])
    parsed = _json_out(result)
    assert parsed["count"] == 2
    assert parsed["devices"] == ["Kitchen Lamp", "Lounge Plug"]


def test_devices_off_requires_a_target():
    with _stub_cli():
        result = _invoke(["devices", "off"])
    assert result.exit_code == 1
    assert "name at least one device" in result.output


# ── devices light ────────────────────────────────────────────────────────


def test_devices_light_previews_the_planned_actions():
    with _stub_cli() as run:
        result = _invoke(
            ["--json", "devices", "light", "Kitchen Lamp", "--on", "--brightness", "60"]
        )
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["device"] == "Kitchen Lamp"
    assert parsed["actions"] == ["turnOn", "setBrightness=60"]
    assert "set_light_state" not in run.seen


def test_devices_light_executes_with_yes():
    captured = {}

    def fake_set(login, entity_id, **kwargs):
        captured.update(entity_id=entity_id, **kwargs)
        return MagicMock(__name__="set_light_state")

    with _stub_cli(results=[{"entityId": "entity-lamp", "actions": ["turnOn", "setColor=red"]}]):
        with patch.object(smarthome_core, "set_light_state", MagicMock(side_effect=fake_set)):
            result = _invoke(
                ["--json", "devices", "light", "Kitchen Lamp", "--color", "Red", "--yes"]
            )
    assert result.exit_code == 0
    assert captured["entity_id"] == "entity-lamp"
    assert captured["color"] == "Red"
    assert captured["power"] is None
    assert _json_out(result)["name"] == "Kitchen Lamp"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--brightness", "500"], "brightness must be between 0 and 100"),
        (["--brightness", "abc"], "brightness must be a number"),
        (["--color", "burnt sienna"], "unknown colour"),
        (["--color-temp", "2700k"], "unknown colour temperature"),
        (["--color", "red", "--color-temp", "warm_white"], "mutually exclusive"),
        ([], "nothing to change"),
    ],
)
def test_devices_light_validates_before_touching_the_network(args, expected):
    """Validation must fail identically with and without --yes, before any login."""
    for argv in ([], ["--yes"]):
        with patch("cli_anything.alexa.alexa_cli._login") as login:
            result = _invoke(["devices", "light", "Kitchen Lamp", *args, *argv])
        assert result.exit_code == 1
        assert expected in result.output
        login.assert_not_called()


def test_devices_light_aborts_on_unknown_device():
    with _stub_cli():
        result = _invoke(["devices", "light", "Nope", "--on"])
    assert result.exit_code == 1
    assert "no device matching" in result.output


# ── guard ────────────────────────────────────────────────────────────────


def test_guard_status_reads_the_panel_by_appliance_id():
    captured = {}

    def fake_fetch(login, appliance_id, name=None):
        captured.update(appliance_id=appliance_id, name=name)
        return MagicMock(__name__="fetch_guard_state")

    with _stub_cli(records=[*_RECORDS, _GUARD],
                   results=[{"name": "Alexa Guard", "armState": "ARMED_STAY", "mode": "home"}]):
        with patch.object(smarthome_core, "fetch_guard_state", MagicMock(side_effect=fake_fetch)):
            result = _invoke(["--json", "guard", "status"])
    assert result.exit_code == 0
    assert captured == {"appliance_id": "APPL-GUARD", "name": "Alexa Guard"}
    assert _json_out(result)["mode"] == "home"


def test_guard_status_aborts_when_the_account_has_no_panel():
    with _stub_cli(records=_RECORDS):
        result = _invoke(["guard", "status"])
    assert result.exit_code == 1
    assert "no Alexa Guard panel" in result.output


def test_guard_set_previews_without_yes():
    with _stub_cli(records=[_GUARD]) as run:
        result = _invoke(["--json", "guard", "set", "away"])
    parsed = _json_out(result)
    assert parsed["dry_run"] is True
    assert parsed["would_set"] == "ARMED_AWAY"
    assert parsed["name"] == "Alexa Guard"
    assert "set_guard_state" not in run.seen


def test_guard_set_executes_with_yes():
    captured = {}

    def fake_set(login, entity_id, state, name=None):
        captured.update(entity_id=entity_id, state=state, name=name)
        return MagicMock(__name__="set_guard_state")

    with _stub_cli(records=[_GUARD], results=[{"armState": "ARMED_STAY"}]):
        with patch.object(smarthome_core, "set_guard_state", MagicMock(side_effect=fake_set)):
            result = _invoke(["--json", "guard", "set", "home", "--yes"])
    assert result.exit_code == 0
    assert captured["state"] == "home"
    assert captured["name"] == "Alexa Guard"
    assert _json_out(result)["armState"] == "ARMED_STAY"


def test_guard_set_rejects_an_unknown_state_at_the_parser():
    result = CliRunner().invoke(cli, ["guard", "set", "disarmed"], obj={})
    assert result.exit_code != 0
    assert "disarmed" in result.output


# ── unaddressable devices (real _run, only the network stubbed) ───────────


@contextlib.contextmanager
def _stub_network(records):
    """Patch only `_login` + the endpoints fetch, leaving the real `_run` in place.

    This is how the ValueError→friendly-abort wiring gets exercised end to end:
    `entity_ref` is a *pure* validator routed through `_run`, so stubbing `_run`
    (as the tests above do) would hide the mapping.
    """
    async def _fetch(_login):
        return list(records)

    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with patch(
            "cli_anything.alexa.alexa_cli.endpoints_core.fetch_endpoint_records", new=_fetch
        ):
            yield


def test_devices_on_aborts_when_the_device_has_no_phoenix_entity_id():
    """A record with no entityId cannot be controlled — say so, don't 200 silently."""
    with _stub_network(_RECORDS):
        result = _invoke(["devices", "on", "Lounge Plug", "--yes"])
    assert result.exit_code == 1
    assert "no phoenix entityId" in result.output


def test_devices_light_aborts_when_the_device_has_no_phoenix_entity_id():
    with _stub_network(_RECORDS):
        result = _invoke(["devices", "light", "Lounge Plug", "--on", "--yes"])
    assert result.exit_code == 1
    assert "no phoenix entityId" in result.output


def test_devices_state_aborts_when_no_selected_device_is_addressable():
    with _stub_network([_RECORDS[1]]):
        result = _invoke(["devices", "state", "--all"])
    assert result.exit_code == 1
    assert "none of the selected devices" in result.output


def test_guard_set_aborts_when_the_panel_has_no_entity_id():
    with _stub_network([{**_GUARD, "entityId": ""}]):
        result = _invoke(["guard", "set", "away", "--yes"])
    assert result.exit_code == 1
    assert "no phoenix entityId" in result.output
