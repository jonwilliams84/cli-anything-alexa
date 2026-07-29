"""Behavioural tests for cli_anything.alexa.alexa_cli pure helpers.

These exercise the ``emit`` formatter (every output branch) and
``_resolve_one_or_abort`` (zero / one / many match paths) using Click's
``CliRunner`` so we assert on the actual stdout/stderr/exit-code behaviour
rather than on source text.  No network or alexapy is involved — the helpers
are pure formatting / resolution logic.
"""

from __future__ import annotations

import asyncio
import json

import click
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import emit, _resolve_one_or_abort, _abort
from cli_anything.alexa.core import endpoints


# ── helpers ─────────────────────────────────────────────────────────────

def _run_with_ctx(callback, as_json: bool = False):
    """Invoke *callback(ctx)* inside a real Click command and return the result."""
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": as_json}
        callback(ctx)

    return runner.invoke(_cmd, [], catch_exceptions=False)


# ── emit ────────────────────────────────────────────────────────────────

def test_emit_json_serialises_and_sorts_keys():
    def _do(ctx):
        emit(ctx, {"b": 1, "a": 2})
    result = _run_with_ctx(_do, as_json=True)
    parsed = json.loads(result.output)
    assert list(parsed.keys()) == ["a", "b"]  # sort_keys=True


def test_emit_none_produces_no_output():
    def _do(ctx):
        emit(ctx, None)
    result = _run_with_ctx(_do, as_json=False)
    assert result.output == ""


def test_emit_string_echoes_directly():
    def _do(ctx):
        emit(ctx, "hello world")
    result = _run_with_ctx(_do, as_json=False)
    assert result.output.strip() == "hello world"


def test_emit_list_of_dicts_renders_table():
    def _do(ctx):
        emit(ctx, [{"name": "Lamp", "id": "1"}, {"name": "Plug", "id": "2"}])
    result = _run_with_ctx(_do, as_json=False)
    assert "name" in result.output
    assert "Lamp" in result.output
    assert "Plug" in result.output


def test_emit_list_of_scalars_prints_each_on_own_line():
    def _do(ctx):
        emit(ctx, ["alpha", "beta", "gamma"])
    result = _run_with_ctx(_do, as_json=False)
    lines = [l for l in result.output.splitlines() if l]
    assert lines == ["alpha", "beta", "gamma"]


def test_emit_dict_with_nested_value_uses_json_for_nested():
    def _do(ctx):
        emit(ctx, {"simple": 42, "nested": {"x": 1}})
    result = _run_with_ctx(_do, as_json=False)
    assert "simple: 42" in result.output
    assert "nested:" in result.output
    assert json.dumps({"x": 1}) in result.output


def test_emit_dict_with_list_value_uses_json_for_list():
    def _do(ctx):
        emit(ctx, {"items": [1, 2, 3]})
    result = _run_with_ctx(_do, as_json=False)
    assert "items:" in result.output
    assert json.dumps([1, 2, 3]) in result.output


def test_emit_fallback_str_for_unknown_type():
    def _do(ctx):
        emit(ctx, 3.14)
    result = _run_with_ctx(_do, as_json=False)
    assert result.output.strip() == "3.14"


# ── _abort ──────────────────────────────────────────────────────────────

def test_abort_writes_error_prefix_and_exits_nonzero():
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        _abort("something broke")

    result = runner.invoke(_cmd, [], catch_exceptions=False)
    assert result.exit_code == 1
    assert "error: something broke" in result.output


# ── _resolve_one_or_abort ───────────────────────────────────────────────

def _rec(name, source="HA", appliance_id="aid1", entity_id="light.kitchen"):
    return {
        "name": name,
        "ha_sourced": source == "HA",
        "manufacturer": "Acme",
        "entity_id": entity_id,
        "applianceId": appliance_id,
        "endpointId": "amzn1.alexa.endpoint." + appliance_id,
    }


def test_resolve_one_or_abort_returns_single_match():
    matches = [_rec("Lamp")]
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": False}
        result = _resolve_one_or_abort(ctx, matches, matches, "Lamp")
        # If we get here, the function returned — emit a marker so we can
        # assert the command completed successfully.
        click.echo("RETURNED:" + result["name"])

    res = runner.invoke(_cmd, [], catch_exceptions=False)
    assert res.exit_code == 0
    assert "RETURNED:Lamp" in res.output


def test_resolve_one_or_abort_zero_matches_aborts():
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": False}
        _resolve_one_or_abort(ctx, [], [], "Nonexistent")

    res = runner.invoke(_cmd, [], catch_exceptions=False)
    assert res.exit_code == 1
    assert "no device matching" in res.output


def test_resolve_one_or_abort_multiple_matches_text_mode_lists_candidates():
    recs = [_rec("Lamp", source="HA", appliance_id="aid_ha"),
            _rec("Lamp", source="native", appliance_id="aid_native", entity_id=None)]
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": False}
        _resolve_one_or_abort(ctx, recs, recs, "Lamp")

    res = runner.invoke(_cmd, [], catch_exceptions=False)
    assert res.exit_code == 1
    assert "matches" in res.output.lower()
    # both candidates should be listed in the table
    assert "aid_ha" in res.output
    assert "aid_native" in res.output


def test_resolve_one_or_abort_multiple_matches_json_mode_emits_structured_error():
    recs = [_rec("Lamp", source="HA", appliance_id="aid_ha"),
            _rec("Lamp", source="native", appliance_id="aid_native", entity_id=None)]
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": True}
        _resolve_one_or_abort(ctx, recs, recs, "Lamp")

    res = runner.invoke(_cmd, [], catch_exceptions=False)
    assert res.exit_code == 1
    # JSON error goes to stderr; CliRunner mixes it into output by default
    parsed = json.loads(res.output)
    assert parsed["error"] == "ambiguous"
    assert parsed["target"] == "Lamp"
    assert len(parsed["matches"]) == 2


# ── endpoints.apply_renames error paths ─────────────────────────────────

def test_apply_renames_missing_endpoint_id_records_error():
    """An entry without endpointId should produce an ok=False result, not crash."""
    planned = [{"old": "A", "new": "B", "endpointId": None}]
    results = asyncio.new_event_loop().run_until_complete(
        endpoints.apply_renames(None, planned)
    )
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "no endpoint id" in results[0]["error"]


def test_apply_renames_empty_plan_returns_empty_list():
    results = asyncio.new_event_loop().run_until_complete(
        endpoints.apply_renames(None, [])
    )
    assert results == []


# ── endpoints.resolve_by_entity edge cases ──────────────────────────────

def test_resolve_by_entity_empty_string_returns_empty():
    assert endpoints.resolve_by_entity([{"entity_id": "light.x"}], "") == []


def test_resolve_by_entity_none_records_returns_empty():
    assert endpoints.resolve_by_entity(None, "light.x") == []


# ── endpoints.find_duplicates: non-ha-only cluster ──────────────────────

def test_find_duplicates_two_native_same_name_no_twin_flag():
    """Two native devices sharing a name: reported but native_plus_ha is False."""
    recs = [
        _rec("Plug", source="native", appliance_id="n1", entity_id=None),
        _rec("Plug", source="native", appliance_id="n2", entity_id=None),
    ]
    dups = endpoints.find_duplicates(recs)
    assert len(dups) == 1
    assert dups[0]["count"] == 2
    assert dups[0]["native_plus_ha"] is False
