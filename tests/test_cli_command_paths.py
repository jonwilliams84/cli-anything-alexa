"""Behavioural tests for cli_anything.alexa.alexa_cli command paths.

Targets uncovered logic in the CLI layer:
  * _require_email aborts when no email is configured
  * _login error paths (AlexaSessionError, generic Exception)
  * _run error paths (AlexaSessionError, ValueError, generic Exception)
  * _emit_bulk_rename_preview (json mode, empty plan, warnings)
  * _resolve_group_members unresolved-entity abort
  * _resolve_child_groups unresolved abort + empty short-circuit
  * _find_group_or_abort no-match abort
  * CLI command dry-run branches (discover, dnd, announce, routines run,
    notifications delete, groups create/add/delete, devices prune/delete)
  * CLI command argument validation (mutually exclusive flags, missing args)
  * _resolve_version fallback when package is not installed
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import (
    _emit_bulk_rename_preview,
    _find_group_or_abort,
    _login,
    _require_email,
    _resolve_child_groups,
    _resolve_group_members,
    _resolve_version,
    _run,
    cli,
)
from cli_anything.alexa.core import session as session_core


# ── helpers ─────────────────────────────────────────────────────────────


def _invoke(cmd, args, obj=None, catch_exceptions=False):
    runner = CliRunner()
    return runner.invoke(cmd, args, obj=obj or {}, catch_exceptions=catch_exceptions)


def _ctx_with(obj_dict):
    """Return a Click context whose obj is pre-populated, for testing helpers."""
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = dict(obj_dict)
        _cmd.ctx = ctx
        click.echo("ok")

    runner.invoke(_cmd, [], catch_exceptions=False)
    return _cmd.ctx


# ── _resolve_version ────────────────────────────────────────────────────


def test_resolve_version_fallback_when_package_not_installed():
    """When the package metadata is absent, the fallback string is returned."""
    from importlib.metadata import PackageNotFoundError

    with patch("cli_anything.alexa.alexa_cli._pkg_version", side_effect=PackageNotFoundError):
        result = _resolve_version()
    assert "unknown" in result


# ── _require_email ──────────────────────────────────────────────────────


def test_require_email_returns_email_when_set():
    ctx = _ctx_with({"email": "user@example.com"})
    assert _require_email(ctx) == "user@example.com"


def test_require_email_aborts_when_missing():
    ctx = _ctx_with({})
    with pytest.raises(SystemExit) as exc_info:
        _require_email(ctx)
    assert exc_info.value.code == 1


# ── _login error paths ─────────────────────────────────────────────────


def test_login_aborts_on_alexa_session_error():
    """_login turns AlexaSessionError into a clean abort (exit 1)."""
    ctx = _ctx_with({"email": "u@example.com"})
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.run_async",
        side_effect=session_core.AlexaSessionError("bad cookie"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _login(ctx)
    assert exc_info.value.code == 1


def test_login_aborts_on_generic_exception():
    """_login turns any unexpected exception into a friendly abort."""
    ctx = _ctx_with({"email": "u@example.com"})
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.run_async", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(SystemExit) as exc_info:
            _login(ctx)
    assert exc_info.value.code == 1


# ── _run error paths ───────────────────────────────────────────────────


def test_run_aborts_on_alexa_session_error():
    ctx = _ctx_with({})
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.run_async",
        side_effect=session_core.AlexaSessionError("session gone"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(ctx, MagicMock())
    assert exc_info.value.code == 1


def test_run_aborts_on_value_error():
    """ValueError is surfaced verbatim (caller-facing messages from core)."""
    ctx = _ctx_with({})
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.run_async",
        side_effect=ValueError("no routine matching 'x'"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(ctx, MagicMock())
    assert exc_info.value.code == 1


def test_run_aborts_on_generic_exception():
    ctx = _ctx_with({})
    with patch(
        "cli_anything.alexa.alexa_cli.session_core.run_async",
        side_effect=ConnectionError("network down"),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(ctx, MagicMock())
    assert exc_info.value.code == 1


# ── _emit_bulk_rename_preview ──────────────────────────────────────────


def test_emit_bulk_rename_preview_json_mode_emits_structured_output():
    ctx_obj = {"as_json": True}
    planned = [{"old": "Lamp-1", "new": "Lamp 1", "source": "pattern 's/-/ /'", "warning": None}]
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = dict(ctx_obj)
        _emit_bulk_rename_preview(ctx, planned, "pattern 's/-/ /'")

    result = runner.invoke(_cmd, [], catch_exceptions=False)
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["mode"] == "pattern 's/-/ /'"
    assert parsed["count"] == 1
    assert parsed["renames"] == planned
    assert "re-run with --yes" in parsed["hint"]


def test_emit_bulk_rename_preview_empty_plan_says_nothing():
    runner = CliRunner()

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": False}
        _emit_bulk_rename_preview(ctx, [], "pattern 's/x/y/'")

    result = runner.invoke(_cmd, [], catch_exceptions=False)
    assert "no devices matched" in result.output
    assert "nothing to rename" in result.output


def test_emit_bulk_rename_preview_text_mode_shows_table_and_warnings():
    runner = CliRunner()
    planned = [
        {"old": "Lamp-1", "new": "Lamp-1!", "source": "map", "warning": "non-speakable"},
        {"old": "Plug", "new": "Plug", "source": "map", "warning": None},
    ]

    @click.command()
    @click.pass_context
    def _cmd(ctx):
        ctx.obj = {"as_json": False}
        _emit_bulk_rename_preview(ctx, planned, "map")

    result = runner.invoke(_cmd, [], catch_exceptions=False)
    assert "Dry-run: 2 rename(s) planned" in result.output
    assert "Lamp-1" in result.output
    assert "DACS warnings" in result.output
    assert "non-speakable" in result.output
    assert "Re-run with --yes to execute" in result.output


# ── _resolve_group_members ─────────────────────────────────────────────


def test_resolve_group_members_aborts_on_unresolved_entities():
    """When an entity can't be resolved to an endpoint, the command aborts."""
    ctx = _ctx_with({})
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._run", return_value={}):
        with patch(
            "cli_anything.alexa.alexa_cli.groups_core.resolve_members",
            return_value=([], ["light.unknown"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_group_members(ctx, mock_login, ("light.unknown",), ())
    assert exc_info.value.code == 1


def test_resolve_group_members_returns_ids_on_success():
    ctx = _ctx_with({})
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._run", return_value={"light.kitchen": "eid-1"}):
        with patch(
            "cli_anything.alexa.alexa_cli.groups_core.resolve_members", return_value=(["eid-1"], [])
        ):
            result = _resolve_group_members(ctx, mock_login, ("light.kitchen",), ())
    assert result == ["eid-1"]


# ── _resolve_child_groups ──────────────────────────────────────────────


def test_resolve_child_groups_empty_short_circuits():
    """No child groups requested → no fetch, returns []."""
    ctx = _ctx_with({})
    result = _resolve_child_groups(ctx, MagicMock(), ())
    assert result == []


def test_resolve_child_groups_aborts_on_unresolved():
    ctx = _ctx_with({})
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
        with patch(
            "cli_anything.alexa.alexa_cli.groups_core.resolve_child_groups",
            return_value=([], ["Nonexistent"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _resolve_child_groups(ctx, mock_login, ("Nonexistent",))
    assert exc_info.value.code == 1


def test_resolve_child_groups_returns_ids_on_success():
    ctx = _ctx_with({})
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
        with patch(
            "cli_anything.alexa.alexa_cli.groups_core.resolve_child_groups",
            return_value=(["gid-1"], []),
        ):
            result = _resolve_child_groups(ctx, mock_login, ("Living Room",))
    assert result == ["gid-1"]


# ── _find_group_or_abort ───────────────────────────────────────────────


def test_find_group_or_abort_aborts_when_no_match():
    ctx = _ctx_with({})
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
        with patch("cli_anything.alexa.alexa_cli.groups_core.find_group", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                _find_group_or_abort(ctx, mock_login, "Nonexistent")
    assert exc_info.value.code == 1


def test_find_group_or_abort_returns_group_on_match():
    ctx = _ctx_with({})
    mock_login = MagicMock()
    group = {"id": "gid-1", "friendlyName": {"value": {"text": "Living Room"}}}
    with patch("cli_anything.alexa.alexa_cli._run", return_value=[group]):
        with patch("cli_anything.alexa.alexa_cli.groups_core.find_group", return_value=group):
            result = _find_group_or_abort(ctx, mock_login, "Living Room")
    assert result["id"] == "gid-1"


# ── CLI command dry-run branches ───────────────────────────────────────


def test_discover_dry_run_without_yes():
    """discover without --yes emits a dry-run preview and does not call the API."""
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        result = _invoke(cli, ["--json", "discover"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_trigger"] == "discovery"


def test_dnd_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        result = _invoke(cli, ["--json", "dnd", "Echo", "on"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["dnd"] == "on"


def test_announce_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        result = _invoke(cli, ["--json", "announce", "hello world"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_announce"] == "hello world"
    assert parsed["device"] == "all"


def test_routines_run_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        result = _invoke(cli, ["--json", "routines", "run", "Good Morning"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_run"] == "Good Morning"


def test_notifications_delete_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        result = _invoke(cli, ["--json", "notifications", "delete", "notif-123"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == "notif-123"


def test_groups_create_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._resolve_group_members", return_value=["eid-1"]):
            with patch("cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]):
                result = _invoke(
                    cli, ["--json", "groups", "create", "Living Room", "--entity", "light.kitchen"]
                )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_create"] == "Living Room"
    assert parsed["memberDeviceIds"] == ["eid-1"]


def test_groups_create_aborts_with_no_members():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._resolve_group_members", return_value=[]):
            with patch("cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]):
                result = _invoke(cli, ["--json", "groups", "create", "Empty"])
    assert result.exit_code == 1
    assert "no members given" in result.output


def test_groups_delete_dry_run_without_yes():
    mock_login = MagicMock()
    group = {"id": "gid-1", "friendlyName": {"value": {"text": "Living Room"}}}
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=group):
            result = _invoke(cli, ["--json", "groups", "delete", "Living Room"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["would_delete"] == "Living Room"
    assert parsed["deviceGroupId"] == "gid-1"


def test_groups_add_dry_run_without_yes():
    mock_login = MagicMock()
    group = {"id": "gid-1", "friendlyName": {"value": {"text": "Living Room"}}}
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=group):
            with patch(
                "cli_anything.alexa.alexa_cli._resolve_group_members", return_value=["eid-1"]
            ):
                with patch("cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]):
                    result = _invoke(
                        cli, ["--json", "groups", "add", "Living Room", "--entity", "light.kitchen"]
                    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert parsed["operation"] == "ADD"
    assert parsed["memberDeviceIds"] == ["eid-1"]


def test_groups_add_aborts_with_nothing_to_change():
    mock_login = MagicMock()
    group = {"id": "gid-1", "friendlyName": {"value": {"text": "Living Room"}}}
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._find_group_or_abort", return_value=group):
            with patch("cli_anything.alexa.alexa_cli._resolve_group_members", return_value=[]):
                with patch("cli_anything.alexa.alexa_cli._resolve_child_groups", return_value=[]):
                    result = _invoke(cli, ["--json", "groups", "add", "Living Room"])
    assert result.exit_code == 1
    assert "nothing to change" in result.output


# ── devices command argument validation ───────────────────────────────


def test_devices_list_mutually_exclusive_filters():
    """--ha-only and --native-only together must abort."""
    result = _invoke(cli, ["--json", "devices", "list", "--ha-only", "--native-only"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


def test_devices_rename_no_target_and_no_bulk_aborts():
    """rename with neither TARGET NEW_NAME nor --pattern/--map must abort."""
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
            result = _invoke(cli, ["--json", "devices", "rename"])
    assert result.exit_code == 1
    assert "give TARGET NEW_NAME" in result.output


def test_devices_delete_no_targets_aborts():
    """delete with no applianceId/entity/name must abort."""
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
            result = _invoke(cli, ["--json", "devices", "delete"])
    assert result.exit_code == 1
    assert "nothing to delete" in result.output


def test_devices_delete_dry_run_without_yes():
    mock_login = MagicMock()
    with patch("cli_anything.alexa.alexa_cli._login", return_value=mock_login):
        with patch("cli_anything.alexa.alexa_cli._run", return_value=[]):
            result = _invoke(cli, ["--json", "devices", "delete", "aid-1"])
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["dry_run"] is True
    assert "aid-1" in parsed["would_delete"]


# ── auth import-pickle validation ──────────────────────────────────────


def test_auth_import_pickle_aborts_without_email():
    """import-pickle with no email configured must abort."""
    result = _invoke(
        cli, ["--json", "auth", "import-pickle", "/tmp/fake.pickle"], obj={"read_in_place": False}
    )
    assert result.exit_code == 1
    assert "email" in result.output.lower()


def test_auth_import_pickle_aborts_with_read_in_place():
    """import-pickle with --cookie-dir set must abort (copying is pointless)."""
    result = _invoke(
        cli,
        [
            "--json",
            "--email",
            "u@example.com",
            "--cookie-dir",
            "/config",
            "auth",
            "import-pickle",
            "/tmp/fake.pickle",
        ],
    )
    assert result.exit_code == 1
    assert "in place" in result.output.lower()


# ── config show / save ─────────────────────────────────────────────────


def test_config_show_emits_safe_keys():
    """config show must not leak config_path or as_json into output."""
    result = _invoke(
        cli,
        ["--json", "--email", "u@example.com", "--url", "amazon.com", "config", "show"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert "email" in parsed
    assert "config_path" not in parsed
    assert "as_json" not in parsed


def test_config_save_persist_and_emit():
    """config save calls project.save_config and emits the path."""
    with patch(
        "cli_anything.alexa.alexa_cli.project.save_config", return_value=Path("/tmp/cfg.json")
    ):
        result = _invoke(
            cli,
            ["--json", "--email", "u@example.com", "config", "save"],
        )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["saved"] == "/tmp/cfg.json"


# ── auth status ────────────────────────────────────────────────────────


def test_auth_status_emits_logged_in_false_and_exits_nonzero():
    """When the cookie is invalid, auth status exits 1."""
    with patch("cli_anything.alexa.alexa_cli.session_core.run_async", return_value=False):
        result = _invoke(
            cli,
            ["--json", "--email", "u@example.com", "--cookie-dir", "/tmp", "auth", "status"],
        )
    assert result.exit_code == 1
    parsed = json.loads(result.output)
    assert parsed["logged_in"] is False


def test_auth_status_emits_logged_in_true():
    with patch("cli_anything.alexa.alexa_cli.session_core.run_async", return_value=True):
        result = _invoke(
            cli,
            ["--json", "--email", "u@example.com", "--cookie-dir", "/tmp", "auth", "status"],
        )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["logged_in"] is True
