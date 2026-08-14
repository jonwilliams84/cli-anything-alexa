"""Behavioural tests for the `kids` CLI paths (Amazon Kids / child mode).

Covers `kids profiles` / `status` / `enable` / `disable`.  Every mutating
command is checked against the harness-wide contract — **preview by default,
act only on --yes** — and assertions are on observable behaviour (exit code,
JSON on stdout, which core coroutine was invoked with what), never on source
text.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_anything.alexa.alexa_cli import cli
from cli_anything.alexa.core import kids as kids_core


def _invoke(args, obj=None):
    return CliRunner().invoke(cli, args, obj=obj or {}, catch_exceptions=False)


def _json_invoke(args):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()):
        result = _invoke(["--json", *args])
    assert result.exit_code == 0, result.output
    return result


@contextlib.contextmanager
def _stub_run(return_value=None, side_effect=None):
    """Patch ``_run`` with a stub that closes the coroutine it is handed."""

    def fake_run(_ctx, coro):
        if hasattr(coro, "close"):
            coro.close()
        if side_effect is not None:
            raise side_effect
        return return_value

    with patch("cli_anything.alexa.alexa_cli._run", side_effect=fake_run) as mock_run:
        yield mock_run


@contextlib.contextmanager
def _stub_core(name):
    with patch.object(kids_core, name, MagicMock()) as stub:
        yield stub


#: (argv, the dry-run field describing the pending action)
MUTATING = [
    (["kids", "enable", "Kitchen", "--child", "Alice"], "would_enable_kids_for"),
    (["kids", "disable", "Kitchen"], "would_disable_kids"),
]


# ── the dry-run contract ────────────────────────────────────────────────


@pytest.mark.parametrize(("argv", "field"), MUTATING, ids=lambda v: str(v)[:40])
def test_kids_mutations_preview_without_yes(argv, field):
    parsed = json.loads(_json_invoke(argv).output)
    assert parsed["dry_run"] is True
    assert field in parsed
    assert "--yes" in parsed["hint"]


@pytest.mark.parametrize("argv", [a for a, _f in MUTATING], ids=lambda v: str(v))
def test_kids_mutations_do_not_reach_the_network_without_yes(argv):
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run() as ran:
        result = _invoke(["--json", *argv])
    assert result.exit_code == 0
    ran.assert_not_called()


@pytest.mark.parametrize("argv", [a for a, _f in MUTATING], ids=lambda v: str(v))
def test_kids_mutations_name_their_target_echo_in_the_preview(argv):
    """Kids mode changes what a speaker does, so the target is never implicit."""
    assert json.loads(_json_invoke(argv).output)["device"] == "Kitchen"


@pytest.mark.parametrize(
    "argv",
    [["kids", "enable", "--child", "Alice"], ["kids", "disable"], ["kids", "enable", "Kitchen"]],
    ids=["enable-no-device", "disable-no-device", "enable-no-child"],
)
def test_kids_mutations_require_an_explicit_target_and_child(argv):
    result = CliRunner().invoke(cli, ["--json", *argv], obj={})
    assert result.exit_code != 0


# ── kids profiles (read-only) ───────────────────────────────────────────


def test_kids_profiles_is_read_only_and_needs_no_yes():
    rows = [{"name": "Alice", "age": 7, "directedId": "amzn1.account.ALICE"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(rows):
        result = _invoke(["--json", "kids", "profiles"])
    assert result.exit_code == 0
    assert json.loads(result.output) == rows


def test_kids_profiles_calls_the_core_fetch():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run([]),
        _stub_core("fetch_profiles") as stub,
    ):
        assert _invoke(["--json", "kids", "profiles"]).exit_code == 0
    stub.assert_called_once()


def test_kids_profiles_renders_a_table_without_json():
    rows = [{"name": "Alice", "age": 7, "directedId": "amzn1.account.ALICE"}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(rows):
        result = _invoke(["kids", "profiles"])
    assert result.exit_code == 0
    assert "Alice" in result.output


# ── kids status (read-only) ─────────────────────────────────────────────


def test_kids_status_with_no_device_covers_every_echo():
    rows = [{"device": "Kitchen", "kids": "on"}, {"device": "Study", "kids": "off"}]
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(rows),
        _stub_core("status_all") as all_stub,
        _stub_core("device_status") as one_stub,
    ):
        result = _invoke(["--json", "kids", "status"])
    assert result.exit_code == 0
    assert json.loads(result.output) == rows
    all_stub.assert_called_once()
    one_stub.assert_not_called()


def test_kids_status_with_a_device_reads_only_that_echo():
    row = {"device": "Study", "kids": "off"}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row),
        _stub_core("status_all") as all_stub,
        _stub_core("device_status") as one_stub,
    ):
        result = _invoke(["--json", "kids", "status", "Study"])
    assert result.exit_code == 0
    assert one_stub.call_args[0][1] == "Study"
    all_stub.assert_not_called()


def test_kids_status_surfaces_unknown_as_null_not_off():
    rows = [{"device": "Kitchen", "kids": None, "child": None}]
    with patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()), _stub_run(rows):
        result = _invoke(["--json", "kids", "status"])
    assert json.loads(result.output)[0]["kids"] is None


# ── kids enable ─────────────────────────────────────────────────────────


def test_kids_enable_preview_echoes_the_child_verbatim():
    parsed = json.loads(_json_invoke(["kids", "enable", "Kitchen", "--child", "id-42"]).output)
    assert parsed["would_enable_kids_for"] == "id-42"


def test_kids_enable_with_yes_calls_the_core_function():
    row = {"device": "Kitchen", "kids": "on", "child": "Alice", "ok": True}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row) as ran,
        _stub_core("enable") as stub,
    ):
        result = _invoke(["--json", "kids", "enable", "Kitchen", "--child", "Alice", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args[0][1:] == ("Kitchen", "Alice")
    assert json.loads(result.output)["ok"] is True


def test_kids_enable_surfaces_a_failed_verify_as_ok_false():
    """The write reports nothing; `ok` comes from the re-read, so it must show."""
    row = {"device": "Kitchen", "kids": "off", "ok": False}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row),
        _stub_core("enable"),
    ):
        result = _invoke(["--json", "kids", "enable", "Kitchen", "--child", "Alice", "--yes"])
    assert json.loads(result.output)["ok"] is False


def test_kids_enable_reports_a_core_value_error_cleanly():
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(side_effect=SystemExit(1)),
        _stub_core("enable"),
    ):
        result = CliRunner().invoke(
            cli,
            ["--json", "kids", "enable", "Kitchen", "--child", "Nobody", "--yes"],
            obj={},
        )
    assert result.exit_code == 1


# ── kids disable ────────────────────────────────────────────────────────


def test_kids_disable_with_yes_calls_the_core_function():
    row = {"device": "Kitchen", "kids": "off", "child": None, "ok": True}
    with (
        patch("cli_anything.alexa.alexa_cli._login", return_value=MagicMock()),
        _stub_run(row) as ran,
        _stub_core("disable") as stub,
    ):
        result = _invoke(["--json", "kids", "disable", "Kitchen", "--yes"])
    assert result.exit_code == 0
    ran.assert_called_once()
    assert stub.call_args[0][1] == "Kitchen"
    assert json.loads(result.output)["ok"] is True


def test_kids_disable_does_not_take_a_child_option():
    result = CliRunner().invoke(
        cli, ["--json", "kids", "disable", "Kitchen", "--child", "Alice"], obj={}
    )
    assert result.exit_code != 0


# ── the group is wired into the CLI ─────────────────────────────────────


def test_kids_group_is_reachable_from_the_root_help():
    result = CliRunner().invoke(cli, ["--help"], obj={})
    assert result.exit_code == 0
    assert "kids" in result.output


@pytest.mark.parametrize("sub", ["profiles", "status", "enable", "disable"])
def test_every_kids_subcommand_has_help(sub):
    result = CliRunner().invoke(cli, ["kids", sub, "--help"], obj={})
    assert result.exit_code == 0
    assert result.output.strip()
