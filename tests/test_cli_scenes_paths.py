"""Behavioural tests for the smart-home scenes CLI paths.

Covers `scenes list` and `scenes activate`. Assertions are on observable
behaviour — exit code, JSON on stdout, and which core coroutine was invoked
with what — never on source text. Every mutating command is checked against
the harness-wide contract: **preview by default, act only on --yes**, and the
executed run reports the same action the preview advertised.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import scenes as scenes_core

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
        # a plain light lives on the same account — scenes must never see it
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
]

_NO_SCENES = [
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
    }
]


@contextlib.contextmanager
def _stub_cli(records=None, results=None):
    """Patch `_login` and `_run` so no network/alexapy is involved.

    `scenes` commands fetch their targets via `scenes_core.fetch_scenes`
    (one `_run` hop), so the mock answers that name with ``records``; every
    other call pops the next value off ``results``. The mock is yielded so
    tests can assert on the coroutine names that were run.
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
        if name == "fetch_scenes":
            # the core fetch filters to scene appliances — mirror it so the
            # stubbed account looks like the real one (a plain light is gone)
            source = records if records is not None else _RECORDS
            return [rec for rec in source if scenes_core.is_scene(rec)]
        if name == "_as_coro":
            # the CLI routes the pure entity_ref validator through _run
            return "entity-movie"
        return queue.pop(0) if queue else {}

    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock:
            mock.seen = seen
            yield mock


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_out(result):
    return json.loads(result.output)


# ── scenes list ──────────────────────────────────────────────────────────


def test_scenes_list_emits_scene_rows_sorted():
    with _stub_cli() as run:
        result = _invoke(["--json", "scenes", "list"])
    assert result.exit_code == 0
    rows = _json_out(result)
    assert [r["name"] for r in rows] == ["Good Night", "Movie Night"]
    assert rows[0]["entityId"] == "entity-goodnight"
    assert rows[0]["source"] == "HA"
    assert "fetch_scenes" in run.seen
    # list is read-only: no entity_ref / activate_scene coroutine ran
    assert "activate_scene" not in run.seen


def test_scenes_list_without_json_still_renders():
    with _stub_cli():
        result = _invoke(["scenes", "list"])
    assert result.exit_code == 0
    assert "Good Night" in result.output


def test_scenes_list_reports_no_scenes_as_an_empty_list():
    with _stub_cli(records=_NO_SCENES):
        result = _invoke(["--json", "scenes", "list"])
    assert result.exit_code == 0
    assert _json_out(result) == []


# ── scenes activate ──────────────────────────────────────────────────────


def test_scenes_activate_is_dry_run_by_default():
    with _stub_cli() as run:
        result = _invoke(["--json", "scenes", "activate", "Movie Night"])
    assert result.exit_code == 0
    body = _json_out(result)
    assert body["dry_run"] is True
    assert body["actions"] == ["Activate"]
    assert body["count"] == 1
    assert body["scenes"] == ["Movie Night"]
    assert "--yes" in body["hint"]
    assert "activate_scene" not in run.seen


def test_scenes_activate_yes_sends_activate_to_one_scene():
    written = {
        "name": "Movie Night",
        "entityId": "entity-movie",
        "action": "Activate",
        "ok": True,
    }
    with _stub_cli(results=[written]) as run:
        result = _invoke(["--json", "scenes", "activate", "movie night", "--yes"])
    assert result.exit_code == 0
    assert _json_out(result) == [written]
    assert "activate_scene" in run.seen


def test_scenes_activate_accepts_an_appliance_id_target():
    written = {"name": "Good Night", "ok": True}
    with _stub_cli(results=[written]):
        result = _invoke(["--json", "scenes", "activate", "SKILL_scene#goodnight", "--yes"])
    assert result.exit_code == 0
    assert _json_out(result) == [written]


def test_scenes_activate_all_executes_every_scene():
    first = {"name": "Good Night", "ok": True}
    second = {"name": "Movie Night", "ok": None}
    with _stub_cli(results=[first, second]):
        result = _invoke(["--json", "scenes", "activate", "--all", "--yes"])
    assert result.exit_code == 0
    rows = _json_out(result)
    assert [r.get("name") for r in rows] == ["Good Night", "Movie Night"]
    assert rows[0]["ok"] is True
    assert rows[1]["ok"] is None  # 'nothing to check' is reported, never a pass


def test_scenes_activate_all_cannot_be_combined_with_targets():
    with _stub_cli():
        result = _invoke(["--json", "scenes", "activate", "Movie Night", "--all"])
    assert result.exit_code == 1


def test_scenes_activate_without_targets_or_all_aborts():
    with _stub_cli():
        result = _invoke(["--json", "scenes", "activate"])
    assert result.exit_code == 1


def test_scenes_activate_aborts_on_a_non_scene_target():
    """Device targeting stays scoped to scenes — a light is not a scene."""
    with _stub_cli():
        result = _invoke(["--json", "scenes", "activate", "Kitchen Lamp"])
    assert result.exit_code == 1


def test_scenes_activate_aborts_on_an_unknown_target():
    with _stub_cli():
        result = _invoke(["--json", "scenes", "activate", "Vanishing Scene"])
    assert result.exit_code == 1


def test_scenes_activate_aborts_when_the_account_has_no_scenes():
    with _stub_cli(records=_NO_SCENES):
        result = _invoke(["--json", "scenes", "activate", "Movie Night"])
    assert result.exit_code == 1


def test_scenes_activate_ambiguous_name_aborts():
    dupes = [_RECORDS[0], dict(_RECORDS[0], endpointId="amzn1.alexa.endpoint.movie2")]
    with _stub_cli(records=dupes):
        result = _invoke(["--json", "scenes", "activate", "Movie Night"])
    assert result.exit_code == 1
